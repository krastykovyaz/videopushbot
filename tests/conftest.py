"""
Tests import bot.py, which has import-time side effects (Telegram session file,
state JSON files, YouTube/VK clients). Run everything inside a throwaway
directory with stub credentials so the real session, tokens and state files
are never touched and no network login happens.
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SANDBOX = Path(tempfile.mkdtemp(prefix="videopushbot-tests-"))

# load_dotenv() never overrides variables that are already set, so these win.
os.environ.update({
    "API_ID": "12345", "API_HASH": "test", "PHONE_NUMBER": "+10000000000",
    "ALLOWED_USERS": "", "VK_ACCESS_TOKEN": "",
    "SECRET_YOUTUBE_FILE_Ru": str(SANDBOX / "missing_ru.json"),
    "SECRET_YOUTUBE_FILE_En": str(SANDBOX / "missing_en.json"),
    "GEMINI_API_KEY": "test", "GOOGLE_API_KEY": "test", "API_KEY": "test",
    "WORKSPACE_DIR": str(SANDBOX / "workspace"),
    "GEMINI_TTS_LANGS": "",          # no real Gemini TTS calls from tests
})
sys.path.insert(0, str(REPO))
os.chdir(SANDBOX)


@pytest.fixture
def bot_mod():
    import bot
    bot.deferred_publishes.clear()
    bot.last_result.clear()
    bot.daily_publish_count.clear()
    bot.job_dedup_pending.clear()
    bot.in_flight_items.clear()
    bot.job_in_flight.clear()
    bot.job_langs_left.clear()
    return bot


def make_job(workspace: Path, job: str, langs=("ru",), webpage=True) -> Path:
    jd = workspace / "8591956842" / job
    for lang in langs:
        (jd / lang).mkdir(parents=True)
        (jd / lang / "output_video.mp4").write_bytes(b"v" * 100)
        (jd / lang / "thumbnail.png").write_bytes(b"t" * 100)
    if webpage:
        (jd / "extracted").mkdir()
        (jd / "extracted" / "text_blocks.json").write_text("[]")
    else:
        (jd / "input.pdf").write_bytes(b"%PDF")
    return jd
