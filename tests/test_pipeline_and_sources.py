import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image


def test_tts_cache_is_keyed_by_text(tmp_path, monkeypatch):
    from pydub import AudioSegment
    from pipeline import step03_tts
    calls = []
    def fake_synth(text, speaker, out_path, lang):
        calls.append(text)
        AudioSegment.silent(duration=200).export(str(out_path), format="wav")
        return True
    monkeypatch.setattr(step03_tts, "_synthesize_sync", fake_synth)
    # the episode MP3 is encoded by system ffmpeg; this test is about cache keys only
    real_export = AudioSegment.export
    def export(self, out_f=None, format="mp3", **kw):
        if format == "mp3":
            Path(out_f).write_bytes(b"mp3")
            return None
        return real_export(self, out_f, format=format, **kw)
    monkeypatch.setattr(AudioSegment, "export", export)
    seg = lambda t: {"segments": [{"speaker": "host1", "text": t}]}
    step03_tts.generate_tts(seg("first script"), tmp_path, "ru")
    step03_tts.generate_tts(seg("first script"), tmp_path, "ru")      # same text: cached
    step03_tts.generate_tts(seg("regenerated script"), tmp_path, "ru")  # /retry: new text
    assert calls == ["first script", "regenerated script"]


def test_frames_cache_is_keyed_by_content(tmp_path):
    from pipeline import step04_frames
    def timeline(text):
        return [{"seg_id": 0, "speaker": "host1", "text": text, "image_path": None, "image_caption": ""}]
    t1, t2 = timeline("old text"), timeline("new text")
    step04_frames.build_frames({"title": "T"}, t1, tmp_path, "ru")
    step04_frames.build_frames({"title": "T\nwith newline"}, t2, tmp_path, "ru")
    assert t1[0]["frame_path"] != t2[0]["frame_path"]
    assert all(Path(t["frame_path"]).exists() for t in (t1[0], t2[0]))
    assert not list(tmp_path.glob("frames/*.tmp"))


def test_concat_holds_previous_frame_when_one_is_missing(tmp_path):
    from pipeline import step05_video
    frames = tmp_path / "frames"; frames.mkdir()
    a = frames / "a.png"; a.write_bytes(b"x")
    timeline = [{"seg_id": 0, "duration_ms": 1000, "frame_path": str(a)},
                {"seg_id": 1, "duration_ms": 2000, "frame_path": str(frames / "missing.png")}]
    step05_video._write_concat(timeline, frames, tmp_path / "concat.txt")
    lines = (tmp_path / "concat.txt").read_text().splitlines()
    durations = [l for l in lines if l.startswith("duration")]
    assert len(durations) == 2                    # the missing segment still gets its time
    assert lines.count(f"file '{a.resolve()}'") == 3


def test_cleanup_protects_deferred_jobs(tmp_path, monkeypatch):
    import cleanup
    ws = tmp_path / "workspace"
    old = time.time() - 10 * 86400
    for job in ("deferred_job", "plain_job"):
        f = ws / "1" / job / "ru" / "output_video.mp4"
        f.parent.mkdir(parents=True); f.write_bytes(b"v")
        os.utime(f, (old, old))
    (tmp_path / "deferred_publishes.json").write_text(json.dumps(
        [{"video_path": str(ws / "1" / "deferred_job" / "ru" / "output_video.mp4")}]))
    monkeypatch.setattr(cleanup, "BASE_DIR", tmp_path)
    monkeypatch.setattr(cleanup, "MEDIA_DIRS", [ws])
    cleanup.clean_media(dry_run=False)
    assert (ws / "1" / "deferred_job" / "ru" / "output_video.mp4").exists()
    assert not (ws / "1" / "plain_job" / "ru" / "output_video.mp4").exists()


@pytest.mark.parametrize("url", ["http://127.0.0.1/x.png", "http://169.254.169.254/latest",
                                 "http://10.0.0.5/a.jpg", "file:///etc/passwd", "ftp://example.com/a"])
def test_non_public_urls_are_refused(url):
    from common import webpage_extract
    assert webpage_extract._is_public_http_url(url) is False


def test_relative_and_proxied_image_urls_resolve():
    from common.webpage_extract import _resolve_image_url
    page = "https://site.example/news/article/"
    assert _resolve_image_url("images/a.jpg", page) == "https://site.example/news/article/images/a.jpg"
    assert _resolve_image_url("//cdn.example/b.jpg", page) == "https://cdn.example/b.jpg"
    assert _resolve_image_url("/_next/image?url=https%3A%2F%2Fcdn.example%2Fc.jpg&w=640", page) == "https://cdn.example/c.jpg"
    assert _resolve_image_url("data:image/png;base64,AAAA", page) is None


class _Resp:
    def __init__(self, data, ctype, status=200, headers=None):
        self.data, self.status_code = data, status
        self.headers = {"Content-Type": ctype, **(headers or {})}
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def raise_for_status(self): pass
    def iter_content(self, chunk_size): yield self.data


def _png_bytes():
    import io
    buf = io.BytesIO(); Image.new("RGB", (200, 200), "red").save(buf, "PNG"); return buf.getvalue() * 1


def test_svg_and_fake_images_are_skipped(tmp_path, monkeypatch):
    from common import webpage_extract
    svg = b"<svg xmlns='http://www.w3.org/2000/svg'>" + b" " * 6000 + b"</svg>"
    monkeypatch.setattr(webpage_extract, "_safe_get", lambda url, **k: _Resp(svg, "image/svg+xml"))
    assert webpage_extract._download_image("https://x/logo", tmp_path, 0) is None
    monkeypatch.setattr(webpage_extract, "_safe_get", lambda url, **k: _Resp(svg, "image/png"))
    assert webpage_extract._download_image("https://x/lies.png", tmp_path, 0) is None   # header lies


def test_real_image_saved_with_its_true_extension(tmp_path, monkeypatch):
    from common import webpage_extract
    import io
    buf = io.BytesIO(); Image.new("RGB", (400, 400)).save(buf, "PNG", compress_level=0)
    monkeypatch.setattr(webpage_extract, "_safe_get", lambda url, **k: _Resp(buf.getvalue(), "image/jpeg"))
    path = webpage_extract._download_image("https://x/photo", tmp_path, 3)
    assert path and path.endswith("web_03.png")


def test_article_links_from_other_hosts_are_ignored(monkeypatch):
    import re
    from common import webpage_extract
    html = ('<a href="https://evil.example/news/abcdef0123456789abcdef01">x</a>'
            '<a href="https://www.site.example/news/abcdef0123456789abcdef02">y</a>'
            '<a href="/news/abcdef0123456789abcdef03">z</a>')
    class R:
        text = html
        def raise_for_status(self): pass
    monkeypatch.setattr(webpage_extract.requests, "get", lambda *a, **k: R())
    links = webpage_extract.find_article_links("https://www.site.example/news", 10,
                                               re.compile(r"^/news/[0-9a-f]{24}$"))
    assert links == ["https://www.site.example/news/abcdef0123456789abcdef02",
                     "https://www.site.example/news/abcdef0123456789abcdef03"]


def test_jpmorgan_marker_ignores_error_pages(monkeypatch):
    from common import pdf_source
    class R:
        def __init__(self, status, ctype):
            self.status_code, self.headers = status, {"Content-Type": ctype, "ETag": '"e1"'}
    monkeypatch.setattr(pdf_source.requests, "head", lambda *a, **k: R(404, "text/html"))
    assert pdf_source.get_pdf_change_marker("https://x/brief.pdf") is None
    monkeypatch.setattr(pdf_source.requests, "head", lambda *a, **k: R(200, "application/pdf"))
    assert pdf_source.get_pdf_change_marker("https://x/brief.pdf").startswith('"e1"')
