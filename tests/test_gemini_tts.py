import base64

from pydub import AudioSegment
from pydub.generators import Sine

from pipeline import gemini_tts


def _speech(ms):            # a loud tone stands in for speech
    return Sine(220).to_audio_segment(duration=ms).apply_gain(-6)


def _dialogue(line_ms, pause_ms=500):
    audio = AudioSegment.silent(duration=200)
    for i, ms in enumerate(line_ms):
        audio += _speech(ms)
        audio += AudioSegment.silent(duration=pause_ms if i < len(line_ms) - 1 else 200)
    return audio


def test_chunks_respect_size_limits():
    lines = [("host1", "x" * 500)] * 9
    chunks = gemini_tts.make_chunks(lines)
    assert all(sum(500 for _ in c) <= gemini_tts.MAX_CHUNK_CHARS for c in chunks)
    assert [i for c in chunks for i in c] == list(range(9))
    assert len(gemini_tts.make_chunks([("host1", "x")] * 20)) == 3   # MAX_CHUNK_LINES = 8


def test_split_cuts_at_the_pauses():
    texts = ["a" * 100, "b" * 50, "c" * 150]            # ~60 ms/char
    audio = _dialogue([6000, 3000, 9000])
    pieces = gemini_tts.split_dialogue(audio, texts)
    assert pieces is not None and len(pieces) == 3
    for piece, expected in zip(pieces, [6000, 3000, 9000]):
        assert abs(len(piece) - expected) < 400


def test_split_refuses_audio_without_pauses():
    audio = _speech(18000)                               # one continuous block, no turn pauses
    assert gemini_tts.split_dialogue(audio, ["a" * 100, "b" * 50, "c" * 150]) is None


def test_split_refuses_audio_that_does_not_match_the_text():
    audio = _dialogue([600, 300])                        # far too short: the model skipped text
    assert gemini_tts.split_dialogue(audio, ["a" * 400, "b" * 400]) is None


class _Resp:
    def __init__(self, status, audio=None):
        self.status_code = status
        self.text = "quota" if status == 429 else ""
        self._audio = audio
    def json(self):
        raw = self._audio.set_frame_rate(24000).set_channels(1).set_sample_width(2).raw_data
        return {"candidates": [{"content": {"parts": [{"inlineData": {
            "mimeType": "audio/L16;codec=pcm;rate=24000", "data": base64.b64encode(raw).decode()}}]}}]}


def test_quota_on_one_model_moves_to_the_next(monkeypatch):
    audio = _dialogue([6000, 3000])
    calls = []
    def fake_post(url, **kw):
        calls.append(url.split("/models/")[1].split(":")[0])
        return _Resp(429) if len(calls) == 1 else _Resp(200, audio)
    monkeypatch.setattr(gemini_tts.requests, "post", fake_post)
    monkeypatch.setattr(gemini_tts, "MIN_REQUEST_GAP_SECONDS", 0)
    monkeypatch.setattr(gemini_tts, "_exhausted_until", {})
    monkeypatch.setattr(gemini_tts, "GEMINI_TTS_MODELS", ["model-a", "model-b"])
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    pieces = gemini_tts.synthesize_chunk([("host1", "a" * 100), ("host2", "b" * 50)], "ru")
    assert calls == ["model-a", "model-b"] and pieces and len(pieces) == 2
    assert "model-a" in gemini_tts._exhausted_until      # not retried again today


def test_generate_tts_uses_gemini_pieces_and_edge_for_the_rest(tmp_path, monkeypatch):
    from pathlib import Path
    from pipeline import step03_tts
    edge_calls = []
    def fake_edge(text, speaker, out_path, lang):
        edge_calls.append(text)
        AudioSegment.silent(duration=300).export(str(out_path), format="wav")
        return True
    def fake_chunk(lines, lang):
        # first chunk succeeds, any later chunk "fails" and must fall back to Edge
        fake_chunk.n = getattr(fake_chunk, "n", 0) + 1
        return [AudioSegment.silent(duration=500) for _ in lines] if fake_chunk.n == 1 else None
    monkeypatch.setattr(step03_tts, "_synthesize_sync", fake_edge)
    monkeypatch.setattr(step03_tts.gemini_tts, "enabled_for", lambda lang: True)
    monkeypatch.setattr(step03_tts.gemini_tts, "synthesize_chunk", fake_chunk)
    monkeypatch.setattr(step03_tts.gemini_tts, "MAX_CHUNK_LINES", 2)
    real_export = AudioSegment.export
    def export(self, out_f=None, format="mp3", **kw):     # skip system ffmpeg for the episode mp3
        if format == "mp3":
            Path(out_f).write_bytes(b"mp3"); return None
        return real_export(self, out_f, format=format, **kw)
    monkeypatch.setattr(AudioSegment, "export", export)

    script = {"segments": [{"speaker": s, "text": t} for s, t in
                           [("host1", "one"), ("host2", "two"), ("host1", "three"), ("host2", "four")]]}
    timeline, fallback = step03_tts.generate_tts(script, tmp_path, "ru")
    assert len(timeline) == 4 and fallback == 0
    assert edge_calls == ["three", "four"]                 # only the failed chunk went to Edge
