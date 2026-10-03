"""
Шаг 3: Озвучка каждого сегмента.

Движки по языку:
  ru → Edge TTS (Microsoft Neural, бесплатно, естественный голос, онлайн)
  en → Chatterbox (MIT, лучшее качество для английского, локально)

Два разных голоса для host1/host2.
Склейка в единый MP3 + timeline.json.
"""

import asyncio
import hashlib
import json
import logging
import os
import uuid
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

# Запасные нейроголоса Edge, если основной голос не отвечает. ru-RU-DmitryNeural
# периодически возвращает "No audio was received" (проверено: 6/12 сегментов
# одного эпизода), а у ru-RU всего два голоса — поэтому для русского берём
# мультиязычные голоса, которые читают по-русски (проверено на сервере).
EDGE_FALLBACK_VOICES = {
    "ru": {
        "host1": os.getenv("EDGE_FALLBACK_HOST1_RU", "en-US-BrianMultilingualNeural"),
        "host2": os.getenv("EDGE_FALLBACK_HOST2_RU", "en-US-EmmaMultilingualNeural"),
    },
    "en": {
        "host1": os.getenv("EDGE_FALLBACK_HOST1_EN", "en-US-GuyNeural"),
        "host2": os.getenv("EDGE_FALLBACK_HOST2_EN", "en-US-AriaNeural"),
    },
}
EDGE_ATTEMPT_TIMEOUT_SECONDS = 60

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

        # Cache key includes the text: /retry regenerates the script into the
        # same folder, and a position-only name reused the OLD audio for new text.
        text_hash = hashlib.sha1(f"{lang}|{speaker}|{text}".encode("utf-8")).hexdigest()[:10]
        wav_path = audio_dir / f"seg_{i:04d}_{speaker}_{text_hash}.wav"

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
            if not _synthesize_sync(text, speaker, wav_path, lang):
                fallback_count += 1
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


def _edge_voice_chain(lang: str, speaker: str) -> list[str]:
    voices = EDGE_VOICES_RU if lang == "ru" else EDGE_VOICES_EN
    primary = voices.get(speaker, list(voices.values())[0])
    fallback = EDGE_FALLBACK_VOICES.get(lang, {}).get(speaker)
    chain = [primary, primary]                 # the primary failure is often transient
    if fallback and fallback != primary:
        chain.append(fallback)
    return chain


def _synthesize_sync(text: str, speaker: str, out_path: Path, lang: str) -> bool:
    """
    Озвучка одного сегмента: Edge TTS (основной голос, повтор, запасной
    нейроголос), и только потом espeak-ng/pyttsx3.

    Каждая попытка Edge пишет в свой уникальный временный файл и запускается
    в отдельном потоке с собственным event loop (asyncio.run() не работает
    внутри loop Telethon). Поток, брошенный по таймауту, может завершиться
    позже — он пишет только в свой временный файл и не затрёт итоговый WAV.

    Возвращает True если сработал Edge TTS, False если пришлось откатиться
    на espeak-ng/pyttsx3.
    """
    import concurrent.futures
    import time as _time

    last_err = None
    for attempt, voice in enumerate(_edge_voice_chain(lang, speaker)):
        if attempt:
            _time.sleep(2)
        tmp_wav = out_path.with_name(f"{out_path.stem}.{uuid.uuid4().hex[:8]}.tmp.wav")

        def run_in_new_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(_synthesize_edge(text, voice, tmp_wav))
            finally:
                loop.close()

        # Managed manually (not `with`): `with`'s implicit shutdown(wait=True)
        # would block on the very call the timeout is meant to give up on.
        ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            ex.submit(run_in_new_loop).result(timeout=EDGE_ATTEMPT_TIMEOUT_SECONDS)
            if not tmp_wav.exists() or tmp_wav.stat().st_size < 1000:
                raise RuntimeError("Edge TTS создал пустой файл")
            os.replace(tmp_wav, out_path)
            log.info(f"Edge TTS OK [{lang}/{speaker}, {voice}]: {out_path.name}")
            return True
        except Exception as e:
            last_err = e
            log.warning(f"Edge TTS ошибка [{lang}/{speaker}, {voice}]: {e}")
        finally:
            ex.shutdown(wait=False)
            tmp_wav.unlink(missing_ok=True)

    log.warning(f"Edge TTS недоступен для [{lang}/{speaker}] ({last_err}), фоллбэк на espeak-ng...")
    if _synthesize_espeak_ng_cli(text, speaker, out_path, lang):
        log.info(f"espeak-ng CLI OK [{lang}/{speaker}]: {out_path.name}")
    else:
        _synthesize_pyttsx3(text, speaker, out_path, lang)
    return False


# ── Edge TTS (RU + EN) ────────────────────────────────────────────────────────

async def _synthesize_edge(text: str, voice: str, out_wav: Path):
    """
    Microsoft Edge TTS — бесплатно, онлайн. Пишет только в out_wav (уникальный
    временный путь, выбранный вызывающим кодом).
    """
    import edge_tts

    mp3_tmp = out_wav.with_suffix(".mp3")
    try:
        communicate = edge_tts.Communicate(text=text, voice=voice, rate="+0%", volume="+0%")
        await communicate.save(str(mp3_tmp))
        AudioSegment.from_mp3(str(mp3_tmp)).export(str(out_wav), format="wav")
    finally:
        mp3_tmp.unlink(missing_ok=True)


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