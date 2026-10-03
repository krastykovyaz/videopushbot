import asyncio
from pathlib import Path

from conftest import make_job

OK = {"success": True, "url": "https://example/v"}
BAD = {"success": False, "error": "invalid_grant"}


def _entry(yt, vk, cat="Auto Detail"):
    return {"video_path": None, "thumb_path": None, "title": "t", "description": "d",
            "category": cat, "youtube": yt, "vk": vk}


def test_fully_published_requires_every_destination(bot_mod):
    bot_mod.vk_uploader = object()
    cases = [("ru", _entry(OK, OK), True), ("ru", _entry(BAD, OK), False),
             ("ru", _entry(OK, BAD), False), ("ru", _entry(None, OK), False),
             ("en", _entry(OK, None), True), ("en", _entry(BAD, None), False)]
    for lang, e, want in cases:
        bot_mod.last_result.clear()
        bot_mod.last_result[f"job:{lang}"] = e
        assert bot_mod._fully_published("job", lang) is want
    assert bot_mod._fully_published("unknown", "ru") is False


def test_remove_published_content_cleans_only_its_job(bot_mod):
    ws = bot_mod.WORKSPACE
    web = make_job(ws, "web", ("ru", "en"), webpage=True)
    bot_mod._remove_published_content(web / "ru")
    assert web.exists() and not (web / "ru").exists() and (web / "en").exists()
    bot_mod._remove_published_content(web / "en")
    assert not web.exists()          # extracted/ no longer blocks removal

    pdf = make_job(ws, "pdf", ("ru",), webpage=False)
    bot_mod._remove_published_content(pdf / "ru")
    assert not pdf.exists()

    # outside the workspace/<user>/<job>/<lang> layout: untouched
    user_dir = ws / "8591956842"
    bot_mod._remove_published_content(user_dir)
    assert user_dir.exists()


def test_remove_keeps_job_still_in_deferred_queue(bot_mod):
    jd = make_job(bot_mod.WORKSPACE, "held", ("ru",))
    bot_mod.deferred_publishes.append({"base_job_id": "held", "lang": "ru"})
    bot_mod._remove_published_content(jd / "ru")
    assert (jd / "ru" / "output_video.mp4").exists()


def _deferred(jd, lang="ru"):
    return {"chat_id": 1, "user_id": 1, "base_job_id": jd.name, "lang": lang,
            "category": "Auto Detail", "video_path": str(jd / lang / "output_video.mp4"),
            "thumb_path": str(jd / lang / "thumbnail.png"), "vk_only": False, "attempts": 0}


def test_flush_drops_entry_whose_files_expired(bot_mod, monkeypatch):
    sent = []
    async def fake_send(*a, **k): sent.append(a)
    monkeypatch.setattr(bot_mod.client, "send_message", fake_send)
    jd = bot_mod.WORKSPACE / "8591956842" / "gone"
    entry = _deferred(jd)
    bot_mod.deferred_publishes.append(entry)
    asyncio.run(bot_mod._flush_one_deferred(entry))
    assert entry not in bot_mod.deferred_publishes and sent


def test_flush_is_silent_while_cap_is_full(bot_mod, monkeypatch):
    calls = []
    async def fake_publish(*a, **k): calls.append(a); return False
    monkeypatch.setattr(bot_mod, "_publish", fake_publish)
    jd = make_job(bot_mod.WORKSPACE, "capped", ("ru",))
    entry = _deferred(jd)
    bot_mod.deferred_publishes.append(entry)
    bot_mod.daily_publish_count[bot_mod._today_key()] = {"ru": bot_mod.DAILY_VIDEO_CAP}
    asyncio.run(bot_mod._flush_one_deferred(entry))
    assert calls == [] and entry in bot_mod.deferred_publishes


def test_flush_backs_off_after_failed_upload_and_publishes_later(bot_mod, monkeypatch):
    jd = make_job(bot_mod.WORKSPACE, "retry", ("en",))
    entry = _deferred(jd, "en")
    bot_mod.deferred_publishes.append(entry)
    key = f"{jd.name}:en"

    async def failing(*a, **k):
        bot_mod.last_result[key] = _entry(BAD, None, "AI Paper Review")
        return True
    monkeypatch.setattr(bot_mod, "_publish", failing)
    asyncio.run(bot_mod._flush_one_deferred(entry))
    assert entry in bot_mod.deferred_publishes and entry["attempts"] == 1 and entry["next_try"] > 0

    entry["next_try"] = 0
    async def ok(*a, **k):
        bot_mod.last_result[key] = _entry(OK, None, "AI Paper Review")
        return True
    monkeypatch.setattr(bot_mod, "_publish", ok)
    asyncio.run(bot_mod._flush_one_deferred(entry))
    assert entry not in bot_mod.deferred_publishes and not jd.exists()


def test_on_job_done_keeps_video_when_publish_crashes(bot_mod, monkeypatch):
    async def fake_send(*a, **k): pass
    async def boom(*a, **k): raise RuntimeError("FloodWait")
    monkeypatch.setattr(bot_mod.client, "send_message", fake_send)
    monkeypatch.setattr(bot_mod, "_publish", boom)
    jd = make_job(bot_mod.WORKSPACE, "crash", ("ru",))
    committed = []
    bot_mod._track_source_item(jd.name, "https://src/1", lambda: committed.append(1))
    bot_mod.job_langs_left[jd.name] = 1

    class J: lang, job_dir, chat_id, user_id = "ru", jd / "ru", 1, 1
    asyncio.run(bot_mod.on_job_done(J, jd / "ru" / "output_video.mp4", jd / "ru" / "thumbnail.png", None))

    assert (jd / "ru" / "output_video.mp4").exists()
    assert any(e["base_job_id"] == jd.name for e in bot_mod.deferred_publishes)
    assert committed == [1]                              # render succeeded -> dedup committed
    assert "https://src/1" not in bot_mod.in_flight_items  # bookkeeping released


def test_failed_render_releases_item_without_committing(bot_mod, monkeypatch):
    async def fake_send(*a, **k): pass
    monkeypatch.setattr(bot_mod.client, "send_message", fake_send)
    jd = make_job(bot_mod.WORKSPACE, "fail", ("ru",))
    committed = []
    bot_mod._track_source_item(jd.name, "https://src/2", lambda: committed.append(1))
    bot_mod.job_langs_left[jd.name] = 1

    class J: lang, job_dir, chat_id, user_id = "ru", jd / "ru", 1, 1
    asyncio.run(bot_mod.on_job_done(J, None, None, "TTS OOM"))
    assert committed == [] and "https://src/2" not in bot_mod.in_flight_items
    assert jd.name not in bot_mod.job_dedup_pending       # next check may pick it up again


def test_checker_last_run_survives_restart(bot_mod):
    bot_mod.checker_last_run.clear()
    bot_mod._mark_checked("ru_auto")
    assert "ru_auto" in bot_mod._load_checker_state()


def test_state_writes_are_atomic(bot_mod, tmp_path):
    target = tmp_path / "state.json"
    bot_mod._atomic_write(target, '{"a": 1}')
    assert target.read_text() == '{"a": 1}' and not (tmp_path / "state.json.tmp").exists()


def test_arxiv_link_requires_real_arxiv_host(bot_mod):
    assert bot_mod._arxiv_link_kind("https://arxiv.org/pdf/2609.11109v1") == "pdf"
    assert bot_mod._arxiv_link_kind("https://arxiv.org/abs/2609.11109") == "abs"
    assert bot_mod._arxiv_link_kind("http://evil.example/?arxiv.org/pdf/2609.11109") is None
    assert bot_mod._arxiv_link_kind("https://arxiv.org.evil.example/pdf/2609.11109") is None
