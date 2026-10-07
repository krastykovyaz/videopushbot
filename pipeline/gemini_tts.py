"""
Gemini TTS for two-host dialogue, on the free tier.

The free tier allows roughly 100 requests/day per TTS model (and ~10/min), so
one request per line (~15 per episode) would hit the limit. Instead a chunk of
consecutive lines goes out as one multi-speaker request — which also sounds
more natural, since the model performs the turn-taking itself — and the
returned audio is cut back into lines at the pauses between them, because the
video switches slides per line and needs each line's duration.

Anything that can't be done cleanly (quota exhausted on every model, a cut
that doesn't line up with the text) returns None, and the caller falls back to
per-line Edge TTS for that chunk.
"""

import base64
import logging
import os
import re
import time
from datetime import datetime, timezone

import requests
from pydub import AudioSegment
from pydub.silence import detect_leading_silence, detect_silence

log = logging.getLogger("gemini_tts")

API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

GEMINI_TTS_LANGS = {l.strip() for l in os.getenv("GEMINI_TTS_LANGS", "ru").split(",") if l.strip()}
# Each model has its own free daily quota, so on a 429 the next one is tried.
GEMINI_TTS_MODELS = [m.strip() for m in os.getenv(
    "GEMINI_TTS_MODELS",
    "gemini-3.8-flash-tts,gemini-3.1-flash-tts-preview,gemini-2.5-flash-preview-tts").split(",") if m.strip()]
VOICES = {
    "host1": os.getenv("GEMINI_TTS_VOICE_HOST1", "Charon"),   # male
    "host2": os.getenv("GEMINI_TTS_VOICE_HOST2", "Kore"),     # female
}
SPEAKER_NAMES = {"host1": "Host1", "host2": "Host2"}

MAX_CHUNK_CHARS = 1800       # ~2 minutes of audio per request
MAX_CHUNK_LINES = 8
MIN_REQUEST_GAP_SECONDS = 7  # stay under ~10 requests/minute
REQUEST_TIMEOUT_SECONDS = 180

_STYLE = {
    "ru": "Озвучь живой диалог двух ведущих научно-популярного подкаста: Host1 — спокойный, уверенный "
          "мужчина, Host2 — любопытная, живая женщина. Читай текст точно, ничего не добавляя.",
    "en": "Read this as a lively two-host science podcast: Host1 is a calm, confident man, Host2 a curious, "
          "lively woman. Read the text exactly, adding nothing.",
}

_exhausted_until: dict[str, str] = {}   # model -> UTC date its daily quota ran out
_last_request_at = 0.0


def _api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("API_KEY")


def enabled_for(lang: str) -> bool:
    return lang in GEMINI_TTS_LANGS and bool(_api_key()) and bool(GEMINI_TTS_MODELS)


def make_chunks(lines: list[tuple[str, str]]) -> list[list[int]]:
    """Groups line indexes into consecutive chunks that fit one request."""
    chunks, current, chars = [], [], 0
    for i, (_, text) in enumerate(lines):
        if current and (chars + len(text) > MAX_CHUNK_CHARS or len(current) >= MAX_CHUNK_LINES):
            chunks.append(current)
            current, chars = [], 0
        current.append(i)
        chars += len(text)
    if current:
        chunks.append(current)
    return chunks


def _request_body(lines: list[tuple[str, str]], lang: str, tagged: bool) -> dict:
    speakers = sorted({s for s, _ in lines})
    if len(speakers) == 1:
        speech = {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICES.get(speakers[0], VOICES["host1"])}}}
        parts = [{"text": "\n".join(t for _, t in lines)}]
    else:
        speech = {"multiSpeakerVoiceConfig": {"speakerVoiceConfigs": [
            {"speaker": SPEAKER_NAMES[s], "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": VOICES[s]}}}
            for s in ("host1", "host2")]}}
        if tagged:
            # gemini-3.8 requires every text part to carry its speaker, and
            # rejects untagged parts (so no separate style instruction).
            parts = [{"text": t, "speechMetadata": {"speaker": SPEAKER_NAMES[s]}} for s, t in lines]
        else:
            parts = [{"text": _STYLE.get(lang, _STYLE["en"]) + "\n" +
                      "\n".join(f"{SPEAKER_NAMES[s]}: {t}" for s, t in lines)}]
    return {"contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": speech}}


def _call_model(model: str, lines: list[tuple[str, str]], lang: str) -> AudioSegment | None:
    """One request (plus one retry with the other request format on a 400).
    Raises _QuotaExhausted on 429."""
    global _last_request_at
    tagged = model.startswith("gemini-3.8")
    for _ in range(2):
        wait = MIN_REQUEST_GAP_SECONDS - (time.time() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.time()
        resp = requests.post(API_URL.format(model=model), params={"key": _api_key()},
                             json=_request_body(lines, lang, tagged), timeout=REQUEST_TIMEOUT_SECONDS)
        if resp.status_code == 429:
            raise _QuotaExhausted(model)
        if resp.status_code == 400 and "speech_metadata" in resp.text and not tagged:
            tagged = True
            continue
        if resp.status_code == 400 and tagged and "speech_metadata" not in resp.text:
            tagged = False
            continue
        if resp.status_code != 200:
            log.warning(f"Gemini TTS {model}: HTTP {resp.status_code} {resp.text[:200]}")
            return None
        try:
            inline = resp.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
        except (KeyError, IndexError, ValueError):
            log.warning(f"Gemini TTS {model}: no audio in response")
            return None
        rate = int((re.search(r"rate=(\d+)", inline.get("mimeType", "")) or [None, 24000])[1])
        return AudioSegment(data=base64.b64decode(inline["data"]), sample_width=2, frame_rate=rate, channels=1)
    return None


class _QuotaExhausted(Exception):
    pass


def _trim(piece: AudioSegment, keep_ms: int = 120) -> AudioSegment:
    thresh = piece.dBFS - 16 if piece.dBFS != float("-inf") else -50
    lead = max(0, detect_leading_silence(piece, silence_threshold=thresh) - keep_ms)
    tail = max(0, detect_leading_silence(piece.reverse(), silence_threshold=thresh) - keep_ms)
    return piece[lead:len(piece) - tail] if len(piece) - lead - tail > 200 else piece


def split_dialogue(audio: AudioSegment, texts: list[str]) -> list[AudioSegment] | None:
    """Cuts one chunk's audio back into per-line pieces. Boundaries are
    estimated from each line's share of the text, then snapped to the nearest
    real pause. Returns None if the cut doesn't line up convincingly."""
    n = len(texts)
    total = len(audio)
    chars = [max(len(t), 1) for t in texts]
    all_chars = sum(chars)
    ms_per_char = total / all_chars
    if not 25 <= ms_per_char <= 200:          # model skipped or padded text
        return None
    if n == 1:
        return [_trim(audio)]

    thresh = audio.dBFS - 16 if audio.dBFS != float("-inf") else -50
    pauses = [(s + e) / 2 for s, e in detect_silence(audio, min_silence_len=180, silence_thresh=thresh)
              if s > 0 and e < total]
    bounds, prev, cum = [], 0.0, 0
    for k in range(n - 1):
        cum += chars[k]
        expected = total * cum / all_chars
        window = max(1500.0, 0.35 * ms_per_char * min(chars[k], chars[k + 1]))
        candidates = [p for p in pauses if p > prev + 400 and abs(p - expected) <= window]
        if not candidates:
            return None
        prev = min(candidates, key=lambda p: abs(p - expected))
        bounds.append(prev)

    edges = [0.0] + bounds + [float(total)]
    pieces = [audio[int(a):int(b)] for a, b in zip(edges, edges[1:])]
    for piece, c in zip(pieces, chars):
        expected = c * ms_per_char
        if abs(len(piece) - expected) > max(1500.0, 0.6 * expected):
            return None
    return [_trim(p) for p in pieces]


def synthesize_chunk(lines: list[tuple[str, str]], lang: str) -> list[AudioSegment] | None:
    """lines: [(speaker, text)]. Returns one AudioSegment per line, or None."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for model in GEMINI_TTS_MODELS:
        if _exhausted_until.get(model) == today:
            continue
        try:
            audio = _call_model(model, lines, lang)
        except _QuotaExhausted:
            _exhausted_until[model] = today
            log.warning(f"Gemini TTS {model}: daily free quota exhausted, trying the next model")
            continue
        except requests.RequestException as e:
            log.warning(f"Gemini TTS {model}: request failed ({e})")
            continue
        if audio is None:
            continue
        pieces = split_dialogue(audio, [t for _, t in lines])
        if pieces is None:
            log.warning(f"Gemini TTS {model}: couldn't cut {len(lines)} lines cleanly, using Edge for this chunk")
            return None
        log.info(f"Gemini TTS OK [{lang}, {model}]: {len(lines)} lines, {len(audio) / 1000:.1f}s")
        return pieces
    return None
