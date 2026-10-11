import logging
import re
import subprocess
import time
from pathlib import Path

import pytest
import requests
from pydub import AudioSegment

from common import gemini_models as gm

KEY = "AIzaSy-test-key-must-never-be-logged-0123456789"
REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    gm._unavailable_until.clear()
    gm._warned.clear()
    monkeypatch.delenv("GEMINI_MODEL_CHAIN", raising=False)
    monkeypatch.delenv("GEMINI_TTS_MODELS", raising=False)


class Resp:
    def __init__(self, status=200, text="OK", version=None, finish="STOP", err=""):
        self.status_code, self._text, self._version, self._finish, self._err = status, text, version, finish, err
        self.text = err
    def json(self):
        if self.status_code != 200:
            return {"error": {"message": self._err}}
        body = {"candidates": [{"content": {"parts": [{"text": self._text}]}, "finishReason": self._finish}]}
        if self._version:
            body["modelVersion"] = self._version
        return body


def fake_api(monkeypatch, behaviour):
    """behaviour(model) -> Resp | Exception. Returns the list of models called."""
    calls = []
    def post(url, headers=None, json=None, timeout=None, params=None):
        assert params is None and "key=" not in url, "the API key must never go in the URL"
        assert headers == {"x-goog-api-key": KEY}
        model = url.rsplit("/", 1)[1].split(":")[0]
        calls.append(model)
        out = behaviour(model)
        if isinstance(out, Exception):
            raise out
        return out
    monkeypatch.setattr(gm.requests, "post", post)
    return calls


# ── the chain itself ──────────────────────────────────────────────────────────
def test_default_chain_follows_the_rules():
    chain = gm.DEFAULT_TEXT_CHAIN
    assert len(chain) == len(set(chain))
    assert not any(m.startswith("gemini-2.0") for m in chain)
    assert "gemini-3.7-flash" not in chain and "gemini-3.5-flash" not in chain   # redirects, not fallbacks
    assert not set(chain) & set(gm.REDIRECTED_TO)
    previews = [i for i, m in enumerate(chain) if gm.is_preview(m)]
    assert previews and previews == list(range(len(chain) - len(previews), len(chain)))   # previews last
    families = {re.match(r"gemini-(\d+\.?\d*)-(flash-lite|flash)", m).groups() for m in chain}
    assert len({f[1] for f in families}) >= 2 and len({f[0] for f in families}) >= 2


def test_dead_models_are_not_in_the_chain():
    assert "gemini-2.5-pro" not in gm.DEFAULT_TEXT_CHAIN and "gemini-2.5-flash-lite" not in gm.DEFAULT_TEXT_CHAIN


def test_preferred_stable_model_goes_first_and_preview_goes_last():
    assert gm.text_chain("gemini-3.6-flash")[0] == "gemini-3.6-flash"
    chain = gm.text_chain("gemini-3-flash-preview")                 # MODEL_EN as a preview: last resort only
    assert chain[0] == "gemini-2.5-flash" and chain[-1] == "gemini-3-flash-preview"
    assert chain.count("gemini-3-flash-preview") == 1


def test_bad_names_from_env_are_replaced_or_dropped(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_MODEL_CHAIN", "gemini-2.0-flash,gemini-3.7-flash,gemini-2.5-flash,gemini-3.8-flash")
    with caplog.at_level(logging.WARNING, logger="gemini_models"):
        chain = gm.text_chain()
    assert chain == ["gemini-3.8-flash", "gemini-2.5-flash"]        # 2.0 dropped, 3.7 -> 3.8, deduped
    assert "retired" in caplog.text and "gemini-3.8-flash" in caplog.text


def test_env_example_models_are_stable_and_in_the_chain():
    text = (REPO / "notebooklm" / ".env.example").read_text()
    for var in ("MODEL_RU", "MODEL_EN"):
        name = re.search(rf"^{var}=(\S+)", text, re.M).group(1)
        assert not gm.is_preview(name) and name in gm.DEFAULT_TEXT_CHAIN


def test_model_names_exist_in_one_place_only():
    out = subprocess.run(["git", "grep", "-nE", r"gemini-[0-9]", "--", "*.py",
                          ":!tests", ":!common/gemini_models.py"], cwd=REPO, capture_output=True, text=True).stdout
    assert out.strip() == "", out


# ── calling the API ───────────────────────────────────────────────────────────
def test_quota_moves_to_the_next_model_and_the_answering_model_is_reported(monkeypatch, caplog):
    calls = fake_api(monkeypatch, lambda m: Resp(429, err="quota") if m == "gemini-2.5-flash"
                     else Resp(200, "hello", version=m))
    with caplog.at_level(logging.INFO, logger="gemini_models"):
        result = gm.generate_text("hi", api_key=KEY)
    assert calls == ["gemini-2.5-flash", "gemini-3.8-flash"] and result.text == "hello"
    assert result.answered == "gemini-3.8-flash"
    assert "Gemini answered: gemini-3.8-flash" in caplog.text


def test_redirected_name_is_flagged_once(monkeypatch, caplog):
    monkeypatch.setenv("GEMINI_MODEL_CHAIN", "gemini-3.6-flash")
    fake_api(monkeypatch, lambda m: Resp(200, "ok", version="gemini-3.9-flash"))
    with caplog.at_level(logging.INFO, logger="gemini_models"):
        gm.generate_text("hi", api_key=KEY)
        gm.generate_text("hi", api_key=KEY)
    assert caplog.text.count("is a redirect") == 1
    assert "answered by gemini-3.9-flash" in caplog.text


def test_404_model_is_warned_about_and_skipped_for_an_hour(monkeypatch, caplog):
    calls = fake_api(monkeypatch, lambda m: Resp(404, err="no longer available to new users")
                     if m == "gemini-2.5-flash" else Resp(200, "ok", version=m))
    with caplog.at_level(logging.WARNING, logger="gemini_models"):
        gm.generate_text("one", api_key=KEY)
        gm.generate_text("two", api_key=KEY)
    assert calls.count("gemini-2.5-flash") == 1                     # not retried on the second request
    warns = [r for r in caplog.records if r.levelno == logging.WARNING and "404" in r.getMessage()]
    assert len(warns) == 1 and "gemini-2.5-flash" in warns[0].getMessage()

    real_time = time.time
    monkeypatch.setattr(gm.time, "time", lambda: real_time() + gm.NOT_FOUND_SKIP_SECONDS + 5)
    gm.generate_text("three", api_key=KEY)
    assert calls.count("gemini-2.5-flash") == 2                     # tried again after the hour


def test_every_model_failing_raises_instead_of_hanging(monkeypatch):
    fake_api(monkeypatch, lambda m: requests.ConnectTimeout("boom"))
    with pytest.raises(gm.GeminiError):
        gm.generate_text("hi", api_key=KEY)


def test_api_key_never_reaches_logs_or_exceptions(monkeypatch, caplog):
    fake_api(monkeypatch, lambda m: requests.ConnectionError(f"HTTPSConnectionPool url=...?key={KEY}"))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(gm.GeminiError) as exc:
            gm.generate_text("hi", api_key=KEY)
    assert KEY not in caplog.text and KEY not in str(exc.value)


def test_empty_response_counts_as_a_failure_and_rotates(monkeypatch):
    calls = fake_api(monkeypatch, lambda m: Resp(200, "", finish="SAFETY") if m == "gemini-2.5-flash"
                     else Resp(200, "fine", version=m))
    assert gm.generate_text("hi", api_key=KEY).text == "fine" and len(calls) == 2


# ── a TTS failure on every model must not lose the job ────────────────────────
@pytest.mark.parametrize("failure", ["404", "429", "500", "timeout"])
def test_tts_failing_on_every_model_falls_back_to_edge(tmp_path, monkeypatch, failure):
    from pipeline import gemini_tts, step03_tts

    def post(url, headers=None, json=None, timeout=None):
        assert "key=" not in url
        if failure == "timeout":
            raise requests.ReadTimeout("slow")
        class R:
            status_code = int(failure); text = "x"
        return R()
    monkeypatch.setattr(gemini_tts.requests, "post", post)
    monkeypatch.setattr(gemini_tts, "MIN_REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(gemini_tts, "_exhausted_until", {})
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    monkeypatch.setattr(gemini_tts, "GEMINI_TTS_LANGS", {"ru"})

    edge = []
    def fake_edge(text, speaker, out_path, lang):
        edge.append(text)
        AudioSegment.silent(duration=300).export(str(out_path), format="wav")
        return True
    monkeypatch.setattr(step03_tts, "_synthesize_sync", fake_edge)
    real_export = AudioSegment.export
    monkeypatch.setattr(AudioSegment, "export", lambda self, out_f=None, format="mp3", **kw:
                        Path(out_f).write_bytes(b"m") if format == "mp3" else real_export(self, out_f, format=format, **kw))

    script = {"segments": [{"speaker": s, "text": t} for s, t in
                           [("host1", "one"), ("host2", "two"), ("host1", "three")]]}
    timeline, fallback = step03_tts.generate_tts(script, tmp_path, "ru")
    assert [e["text"] for e in timeline] == ["one", "two", "three"]    # the job completed
    assert edge == ["one", "two", "three"]                              # every line went through Edge


def test_tts_404_model_is_skipped_for_an_hour(monkeypatch):
    from pipeline import gemini_tts
    calls = []
    def post(url, headers=None, json=None, timeout=None):
        calls.append(url.rsplit("/", 1)[1].split(":")[0])
        class R:
            status_code = 404; text = "gone"
        return R()
    monkeypatch.setattr(gemini_tts.requests, "post", post)
    monkeypatch.setattr(gemini_tts, "MIN_REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(gemini_tts, "_exhausted_until", {})
    monkeypatch.setenv("GEMINI_API_KEY", KEY)
    lines = [("host1", "a" * 100), ("host2", "b" * 100)]
    assert gemini_tts.synthesize_chunk(lines, "ru") is None
    first = list(calls)
    assert gemini_tts.synthesize_chunk(lines, "ru") is None
    assert calls == first and first == gm.DEFAULT_TTS_MODELS            # each tried once, then skipped
