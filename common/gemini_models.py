"""
The one place that knows Gemini model names, calls the text API, and tracks
which models are currently unusable.

Why REST instead of the (deprecated) google-generativeai SDK: Google now
redirects retired models to newer ones behind the scenes (gemini-3.5-flash is
answered by gemini-3.6-flash, gemini-3.7-flash by gemini-3.8-flash). The
response's `modelVersion` says which model really answered, but the old SDK
does not expose it, so a "fallback" to a redirected name silently retried the
same backend. REST also lets the API key travel in a header, so it can never
end up in a logged URL or exception message.

Model lists are edited here (or via the env vars below) and nowhere else.
"""

import logging
import os
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger("gemini_models")

API_ROOT = "https://generativelanguage.googleapis.com/v1beta/models"

# Stable models first, previews last, from more than one family (2.5 flash,
# 3.x flash, flash-lite). Every name was checked with a real request: each
# answers as itself, and each has its own free-tier quota, so on a 429 the
# next entry is a genuinely different backend.
DEFAULT_TEXT_CHAIN = [
    "gemini-2.5-flash",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-3-flash-preview",
]

# Names that still resolve but are the same backend as another entry, so they
# are not real fallbacks. If one shows up via .env it is replaced.
REDIRECTED_TO = {
    "gemini-3.5-flash": "gemini-3.6-flash",
    "gemini-3.7-flash": "gemini-3.8-flash",
}
RETIRED_PREFIXES = ("gemini-2.0",)       # gone from the API

DEFAULT_TTS_MODELS = [
    "gemini-3.8-flash-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
]
# gemini-3.8 TTS rejects a multi-speaker request unless every text part is
# tagged with its speaker; older TTS models take a single prompt.
_TTS_TAGGED_PREFIXES = ("gemini-3.8",)

NOT_FOUND_SKIP_SECONDS = 3600

_lock = threading.Lock()
_unavailable_until: dict[str, float] = {}
_warned: set[tuple] = set()


class GeminiError(Exception):
    pass


class GeminiNotFound(GeminiError):
    pass


class GeminiQuota(GeminiError):
    pass


@dataclass
class GeminiResult:
    text: str
    requested: str
    answered: str


def _warn_once(key: tuple, message: str):
    with _lock:
        if key in _warned:
            return
        _warned.add(key)
    log.warning(message)


def api_key() -> str | None:
    return os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("API_KEY")


def is_preview(name: str) -> bool:
    return "preview" in name


def _clean(names: list[str]) -> list[str]:
    out = []
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        if name.startswith(RETIRED_PREFIXES):
            _warn_once(("retired", name), f"Gemini model {name} is retired and was ignored — remove it from .env")
            continue
        if name in REDIRECTED_TO:
            real = REDIRECTED_TO[name]
            _warn_once(("redirect-cfg", name),
                       f"Gemini model {name} is answered by {real}; using {real} instead — update .env")
            name = real
        if name not in out:
            out.append(name)
    return out


def text_chain(preferred: str | None = None, exclude=frozenset()) -> list[str]:
    """Ordered models to try for one request: the preferred one first (when it
    is a stable model), then the rest; previews always last; models that
    returned 404 recently are skipped."""
    env = os.getenv("GEMINI_MODEL_CHAIN", "")
    base = [m for m in env.split(",") if m.strip()] or DEFAULT_TEXT_CHAIN
    names = _clean(([preferred] if preferred else []) + list(base))
    ordered = [m for m in names if not is_preview(m)] + [m for m in names if is_preview(m)]
    now = time.time()
    with _lock:
        return [m for m in ordered if _unavailable_until.get(m, 0) <= now and m not in exclude]


def tts_models() -> list[str]:
    env = os.getenv("GEMINI_TTS_MODELS", "")
    names = [m.strip() for m in env.split(",") if m.strip()] or DEFAULT_TTS_MODELS
    now = time.time()
    with _lock:
        return [m for m in names if _unavailable_until.get(m, 0) <= now]


def tts_needs_tagged_parts(model: str) -> bool:
    return model.startswith(_TTS_TAGGED_PREFIXES)


def mark_not_found(model: str):
    """A model that answers 404 is not retried on every request — skipped for
    an hour, then tried again in case it came back."""
    with _lock:
        _unavailable_until[model] = time.time() + NOT_FOUND_SKIP_SECONDS
    log.warning(f"Gemini model {model} returned 404 (not found / no longer available) — "
                f"skipping it for {NOT_FOUND_SKIP_SECONDS // 60} min")


def _error_message(resp) -> str:
    try:
        return resp.json()["error"]["message"][:200]
    except Exception:
        return (resp.text or "")[:200]


def _generate_one(model: str, prompt: str, key: str, timeout: int, json_mode: bool = False) -> GeminiResult:
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    if json_mode:
        # JSON mode guarantees syntactically valid JSON (weaker models otherwise
        # sometimes emit unescaped quotes or stop mid-object); the large output
        # budget keeps a long script from being cut off.
        body["generationConfig"] = {"responseMimeType": "application/json", "maxOutputTokens": 16384}
    try:
        resp = requests.post(
            f"{API_ROOT}/{model}:generateContent",
            headers={"x-goog-api-key": key},
            json=body,
            timeout=timeout,
        )
    except requests.RequestException as e:
        # type name only: never format the exception (it can echo request details)
        raise GeminiError(f"request failed ({type(e).__name__})") from None

    if resp.status_code == 404:
        raise GeminiNotFound(_error_message(resp))
    if resp.status_code == 429:
        raise GeminiQuota(_error_message(resp))
    if resp.status_code != 200:
        raise GeminiError(f"HTTP {resp.status_code}: {_error_message(resp)}")

    data = resp.json()
    candidates = data.get("candidates") or []
    parts = (candidates[0].get("content") or {}).get("parts") or [] if candidates else []
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        reason = (candidates[0].get("finishReason") if candidates else None) \
            or (data.get("promptFeedback") or {}).get("blockReason") or "no candidates"
        raise GeminiError(f"empty response ({reason})")
    if candidates[0].get("finishReason") == "MAX_TOKENS":
        log.warning(f"Gemini {model}: output was cut off at the token limit")
    return GeminiResult(text=text, requested=model, answered=data.get("modelVersion") or model)


def generate_text(prompt: str, api_key: str | None = None, preferred: str | None = None,
                  timeout: int = 60, json_mode: bool = False, exclude=frozenset()) -> GeminiResult:
    """Tries the chain in order; returns the first answer, saying which model
    really gave it. `exclude` skips models the caller already got an unusable
    answer from. Raises the last error if every model fails."""
    key = api_key or globals()["api_key"]()
    if not key:
        raise GeminiError("no Gemini API key configured")
    chain = text_chain(preferred, exclude)
    if not chain:
        raise GeminiError("no Gemini model is currently available")

    last_err: Exception | None = None
    for model in chain:
        try:
            result = _generate_one(model, prompt, key, timeout, json_mode)
        except GeminiNotFound as e:
            mark_not_found(model)
            last_err = e
            continue
        except GeminiQuota:
            log.warning(f"Gemini {model}: quota exhausted, trying the next model")
            last_err = GeminiQuota(f"{model}: quota exhausted")
            continue
        except GeminiError as e:
            log.warning(f"Gemini {model}: {e}, trying the next model")
            last_err = e
            continue

        if result.answered != model:
            _warn_once(("redirect", model, result.answered),
                       f"Gemini {model} was answered by {result.answered} — the name is a redirect, "
                       f"replace it in the chain with {result.answered}")
        log.info(f"Gemini answered: {result.answered}" +
                 (f" (requested {model})" if result.answered != model else ""))
        return result
    raise last_err or GeminiError("all Gemini models failed")
