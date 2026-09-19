"""
Шаг 3: Озвучка каждого сегмента.

Движки по языку:
  ru → Edge TTS (Microsoft Neural, бесплатно, естественный голос, онлайн)
  en → Chatterbox (MIT, лучшее качество для английского, локально)

Два разных голоса для host1/host2.
Склейка в единый MP3 + timeline.json.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

from pydub import AudioSegment

log = logging.getLogger("step03")

PAUSE_BETWEEN_MS = 300

# Edge TTS голоса для русского — Microsoft Neural, очень естественные
EDGE_VOICES_RU = {
    "host1": os.getenv("EDGE_VOICE_HOST1_RU", "ru-RU-DmitryNeural"),   # мужской
    "host2": os.getenv("EDGE_VOICE_HOST2_RU", "ru-RU-SvetlanaNeural"), # женский
}

# Edge TTS голоса для английского
EDGE_VOICES_EN = {
    "host1": os.getenv("EDGE_VOICE_HOST1_EN", "en-US-AndrewNeural"),
    "host2": os.getenv("EDGE_VOICE_HOST2_EN", "en-US-JennyNeural"),
}

# Chatterbox: опциональные референсные WAV (EN фоллбэк)
CHATTERBOX_REF = {
    "host1": os.getenv("VOICE_HOST1_REF", ""),
    "host2": os.getenv("VOICE_HOST2_REF", ""),
}

_CHATTERBOX_CACHE = None


# ── Публичный интерфейс ───────────────────────────────────────────────────────

def generate_tts(script: dict, job_dir: Path, lang: str = "ru") -> list[dict]:
    """
    Генерирует WAV для каждого сегмента, склеивает в final_audio.mp3.
    Возвращает timeline.
    """
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(exist_ok=True)

    segments = script.get("segments", [])
    timeline = []
    combined = AudioSegment.empty()
    current_ms = 0
    fallback_count = 0

    for i, seg in enumerate(segments):
        speaker = seg.get("speaker", "host1")
        text    = seg.get("text", "").strip()
        if not text:
            continue

        wav_path = audio_dir / f"seg_{i:04d}_{speaker}.wav"

        if not wav_path.exists():
            log.info(f"TTS [{lang}/{speaker}] сег {i+1}/{len(segments)}: {text[:60]}...")
            if not _synthesize_sync(text, speaker, wav_path, lang):
                fallback_count += 1
        else:
            log.info(f"Кэш: сегмент {i+1}")

        try:
            seg_audio = AudioSegment.from_wav(str(wav_path))
        except Exception as e:
            # Битый/невалидный кэш (например, старый AIFF-как-.wav) — перегенерировать.
            log.warning(f"Кэш сегмента {i+1} повреждён ({e}), перегенерирую...")
            wav_path.unlink(missing_ok=True)
            _synthesize_sync(text, speaker, wav_path, lang)
            seg_audio = AudioSegment.from_wav(str(wav_path))
        duration_ms = len(seg_audio)

        timeline.append({
            "seg_id":        i,
            "speaker":       speaker,
            "text":          text,
            "start_ms":      current_ms,
            "end_ms":        current_ms + duration_ms,
            "duration_ms":   duration_ms,
            "wav_path":      str(wav_path),
            "image_path":    seg.get("image_path"),
            "image_caption": seg.get("image_caption", ""),
        })

        # Нормализовать громкость сегмента перед склейкой
        seg_audio = seg_audio.normalize()
        combined += seg_audio
        combined += AudioSegment.silent(duration=PAUSE_BETWEEN_MS)
        current_ms += duration_ms + PAUSE_BETWEEN_MS

    final_mp3 = job_dir / "final_audio.mp3"
    combined.export(
        str(final_mp3),
        format="mp3",
        bitrate="192k",
        parameters=["-ar", "44100", "-ac", "2"]   # 44.1kHz стерео вместо моно 24kHz
    )
    log.info(f"Аудио: {len(combined)/1000:.1f}с → {final_mp3}")

    timeline_path = job_dir / "timeline.json"
    with open(timeline_path, "w", encoding="utf-8") as f:
        json.dump(timeline, f, ensure_ascii=False, indent=2)

    if fallback_count:
        log.warning(f"{fallback_count}/{len(timeline)} сегментов озвучены фоллбэком "
                     f"(espeak-ng/pyttsx3) вместо Edge TTS — голос местами может звучать роботизированно")

    return timeline, fallback_count


def _synthesize_sync(text: str, speaker: str, out_path: Path, lang: str) -> bool:
    """
    Синхронная обёртка для async Edge TTS.
    asyncio.run() не работает внутри уже запущенного loop (Telethon).
    Используем отдельный поток с новым event loop.

    Возвращает True если сработал Edge TTS, False если пришлось откатиться
    на espeak-ng/pyttsx3 (нужно вызывающему коду, чтобы посчитать деградацию
    качества озвучки за весь эпизод).
    """
    import concurrent.futures

    def run_in_new_loop():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(
                _synthesize_edge(text, speaker, out_path, lang)
            )
        finally:
            loop.close()

    # Managed manually (not `with`) so a timeout can abandon the background
    # thread via shutdown(wait=False) — `with`'s implicit shutdown(wait=True)
    # blocks on the SAME hung call the timeout was meant to give up on,
    # making the 120s timeout a no-op and freezing the single worker on it.
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = ex.submit(run_in_new_loop)
        future.result(timeout=120)
        ex.shutdown(wait=False)
        # Проверить что файл реально создался и не пустой
        if not out_path.exists() or out_path.stat().st_size < 1000:
            raise RuntimeError(f"Edge TTS создал пустой файл: {out_path}")
        log.info(f"Edge TTS OK [{lang}/{speaker}]: {out_path.name}")
        return True
    except Exception as e:
        ex.shutdown(wait=False)
        log.warning(f"Edge TTS ошибка [{lang}/{speaker}]: {e}, фоллбэк...")
        if _synthesize_espeak_ng_cli(text, speaker, out_path, lang):
            log.info(f"espeak-ng CLI OK [{lang}/{speaker}]: {out_path.name}")
        else:
            _synthesize_pyttsx3(text, speaker, out_path, lang)
        return False


# ── Edge TTS (RU + EN) ────────────────────────────────────────────────────────

async def _synthesize_edge(text: str, speaker: str, out_path: Path, lang: str):
    """
    Microsoft Edge TTS — бесплатно, онлайн, качество почти как платный ElevenLabs.
    Голоса: ru-RU-DmitryNeural, ru-RU-SvetlanaNeural, en-US-AndrewNeural, etc.
    pip install edge-tts
    """
    import edge_tts

    voices = EDGE_VOICES_RU if lang == "ru" else EDGE_VOICES_EN
    voice  = voices.get(speaker, list(voices.values())[0])

    # Сохранить как MP3, потом конвертим в WAV через pydub
    mp3_tmp = out_path.with_suffix(".tmp.mp3")

    communicate = edge_tts.Communicate(text=text, voice=voice, rate="+0%", volume="+0%")
    await communicate.save(str(mp3_tmp))

    # MP3 → WAV (pydub)
    audio = AudioSegment.from_mp3(str(mp3_tmp))
    audio.export(str(out_path), format="wav")
    mp3_tmp.unlink(missing_ok=True)
    log.info(f"Edge TTS OK [{voice}]: {out_path.name}")


# ── Chatterbox (EN фоллбэк) ───────────────────────────────────────────────────

def _load_chatterbox():
    global _CHATTERBOX_CACHE
    if _CHATTERBOX_CACHE is None:
        try:
            from chatterbox.tts import ChatterboxTTS
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            _CHATTERBOX_CACHE = ChatterboxTTS.from_pretrained(device=device)
            log.info(f"Chatterbox загружен ({device})")
        except ImportError:
            _CHATTERBOX_CACHE = None
    return _CHATTERBOX_CACHE


def _synthesize_chatterbox(text: str, speaker: str, out_path: Path):
    model = _load_chatterbox()
    if model:
        try:
            import torchaudio
            ref = CHATTERBOX_REF.get(speaker) or None
            if ref and not Path(ref).exists():
                ref = None
            wav = model.generate(text, audio_prompt_path=ref,
                                 exaggeration=0.4, cfg_weight=0.5)
            torchaudio.save(str(out_path), wav, model.sr)
            return
        except Exception as e:
            log.warning(f"Chatterbox ошибка: {e}")
    if not _synthesize_espeak_ng_cli(text, speaker, out_path, lang="en"):
        _synthesize_pyttsx3(text, speaker, out_path, lang="en")


# ── espeak-ng CLI (более надёжный фоллбэк на Linux) ──────────────────────────

def _synthesize_espeak_ng_cli(text: str, speaker: str, out_path: Path, lang: str = "ru") -> bool:
    """
    Синтезирует через сам бинарник espeak-ng, в обход pyttsx3/ctypes.
    pyttsx3's espeak-driver написан под классический espeak и на Linux
    иногда падает с "SetVoiceByName failed with unknown return code -1"
    из-за несовпадения именования голосов с espeak-ng (напр. "gmw/en").
    Прямой вызов CLI этой проблемы не имеет.
    Возвращает True при успехе, False — если espeak-ng не установлен или
    вызов не удался (тогда вызывающий код падает обратно на pyttsx3).
    """
    import shutil
    import subprocess

    if not shutil.which("espeak-ng"):
        return False

    voice = "ru" if lang == "ru" else "en-us"
    variant = "m3" if speaker == "host1" else "f3"
    try:
        subprocess.run(
            ["espeak-ng", "-v", f"{voice}+{variant}", "-s", "155", "-w", str(out_path), text],
            check=True, capture_output=True, timeout=120,
        )
        return out_path.exists() and out_path.stat().st_size > 1000
    except subprocess.CalledProcessError as e:
        log.warning(f"espeak-ng CLI ошибка: {e.stderr.decode(errors='replace')[:200]}")
        return False
    except subprocess.TimeoutExpired:
        log.warning("espeak-ng CLI: таймаут")
        return False


# ── pyttsx3 (финальный фоллбэк) ───────────────────────────────────────────────

def _synthesize_pyttsx3(text: str, speaker: str, out_path: Path, lang: str = "ru"):
    try:
        import pyttsx3
        engine = pyttsx3.init()
        voices = engine.getProperty("voices")
        lang_tag = "ru" if lang == "ru" else "en"
        selected = None
        for v in voices:
            vid = (v.id or "").lower()
            if lang_tag in vid:
                if speaker == "host2" and selected:
                    selected = v; break
                selected = v
        if not selected and voices:
            idx = 0 if speaker == "host1" else min(1, len(voices)-1)
            selected = voices[idx]
        if selected:
            try:
                engine.setProperty("voice", selected.id)
            except Exception as e:
                # Не даём сбою выбора голоса убить весь сегмент — синтезируем
                # голосом по умолчанию движка, лучше так, чем ничего.
                log.warning(f"pyttsx3: не удалось выбрать голос {selected.id!r} ({e}), использую голос по умолчанию")
        engine.setProperty("rate", 155)
        engine.save_to_file(text, str(out_path))
        engine.runAndWait()
        _fix_if_actually_aiff(out_path)
    except Exception as e:
        log.error(f"pyttsx3 ошибка: {e}")
        raise


def _fix_if_actually_aiff(wav_path: Path):
    """
    На macOS pyttsx3 (движок NSSpeechSynthesizer) пишет AIFF-данные даже
    в файл с расширением .wav — ffmpeg потом падает с "invalid start code
    FORM in RIFF header". Если это произошло, перекодируем в настоящий WAV.
    """
    try:
        with open(wav_path, "rb") as f:
            header = f.read(4)
        if header != b"FORM":
            return  # уже нормальный RIFF/WAV
        log.warning(f"pyttsx3 записал AIFF вместо WAV ({wav_path.name}), перекодирую...")
        audio = AudioSegment.from_file(str(wav_path), format="aiff")
        audio.export(str(wav_path), format="wav")
    except Exception as e:
        log.error(f"Не удалось перекодировать AIFF→WAV ({wav_path.name}): {e}")
        raise