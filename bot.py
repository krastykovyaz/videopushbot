"""
NotebookLM Clone — Telethon userbot
Принимает PDF в личку, автоматически определяет тему и язык(и),
генерирует видео и публикует его на YouTube/VK без ручных шагов.
"""

import asyncio
import json
import os
import re
import shutil
import logging
from pathlib import Path
from datetime import datetime
from typing import Callable

from dotenv import load_dotenv
from telethon import TelegramClient, events, helpers as tl_helpers
from telethon.tl.functions.channels import JoinChannelRequest

import config_en
import config_ru2
from common.gemini_client import GeminiContentGenerator, append_footer
from common.metadata import load_script_title_and_points
from common.patreon_post import format_patreon_post
from common.pdf_source import download_pdf, is_suitable_pdf_url, get_pdf_change_marker
from common.vk_uploader import VKUploader
from common.webpage_extract import (
    extract_webpage, find_article_links, find_ru_auto_links, extract_page_title,
    find_quarterly_digest_candidates,
)
from common.youtube_uploader import YouTubeUploader
from pipeline.step01_extract import extract_pdf
from queue_worker import JobQueue, Job

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("bot")

# ── Конфиг ──────────────────────────────────────────────────────────────────
OWNER_ID = 8591956842
# Companion channels posting the SAME daily papers in different languages —
# each triggers generation of ONLY its own language (not both), since they
# cover the same paper and would otherwise duplicate every upload.
ARXIV_CHANNEL_EN = "arxivpaper"
ARXIV_CHANNEL_RU = "arxivpaperu"
# Digest posts ("Top N papers of the Week/Month") re-list papers already
# covered by earlier daily posts. Tracked per-language (not globally) — RU
# and EN each need their own video for the same paper, only reprocessing
# within the SAME language should be prevented.
PROCESSED_ARXIV_FILE = Path("processed_arxiv_papers.json")

# Daily crypto-newsletter check: new articles get BOTH ru+en videos (this
# content is never automotive, so the usual ru-only exception never applies).
COINDESK_NEWSLETTERS = [
    "https://www.coindesk.com/newsletters/crypto-long-short",
    "https://www.coindesk.com/newsletters/state-of-crypto",
]
COINDESK_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
PROCESSED_COINDESK_FILE = Path("processed_coindesk_articles.json")

# Daily RU auto-news pick: one story a day, chosen by Gemini from fresh
# candidates across za rulem / kolesa.ru / autonews.ru. RU-only, always
# routed to Auto Detail (matches the existing automotive routing rule).
RU_AUTO_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
PROCESSED_RU_AUTO_FILE = Path("processed_ru_auto_stories.json")

# Quarterly all-countries car-import digest ("итоги квартала"): rare,
# event-driven articles, so checked every few days rather than daily —
# frequent enough to catch one within a few days of publication.
QUARTERLY_DIGEST_CHECK_INTERVAL_SECONDS = 3 * 24 * 60 * 60
PROCESSED_QUARTERLY_DIGEST_FILE = Path("processed_quarterly_digests.json")

# Weekly JPMorgan Asset Management market brief: a stable URL whose PDF is
# replaced in place each week (not a new URL per report like the other
# sources), so dedup compares an ETag/Last-Modified marker instead of a URL.
# Finance content, never automotive, so it always gets both ru+en videos.
JPMORGAN_WEEKLY_BRIEF_URL = (
    "https://am.jpmorgan.com/content/dam/jpm-am-aem/emea/regional/en/insights/"
    "market-insights/the-weekly-brief/mi-weekly-market-brief-en.pdf"
)
JPMORGAN_CHECK_INTERVAL_SECONDS = 24 * 60 * 60
PROCESSED_JPMORGAN_FILE = Path("processed_jpmorgan_brief.json")

# Hard cap on how many videos actually get uploaded per channel per day —
# protects against bursts (e.g. several auto-sources catching up on a
# backlog at once after downtime) flooding a channel in a short window.
# Applies to every source, manual PDFs included.
DAILY_VIDEO_CAP = 2
DAILY_PUBLISH_COUNT_FILE = Path("daily_publish_count.json")

API_ID       = int(os.getenv("API_ID"))
API_HASH     = os.getenv("API_HASH")
PHONE_NUMBER = os.getenv("PHONE_NUMBER")
WORKSPACE    = Path(os.getenv("WORKSPACE_DIR", "./workspace"))
DEFAULT_LANG = os.getenv("DEFAULT_LANG", "ru")
MAX_WORKERS  = int(os.getenv("MAX_WORKERS", 1))

_raw = os.getenv("ALLOWED_USERS", "").strip()
ALLOWED_USERS = set(int(x) for x in _raw.split(",") if x) if _raw else None

WORKSPACE.mkdir(parents=True, exist_ok=True)

# ── Клиент и очередь ────────────────────────────────────────────────────────
client    = TelegramClient("notebooklm_session", API_ID, API_HASH)
job_queue = JobQueue(max_workers=MAX_WORKERS)

def _load_processed_arxiv() -> dict:
    if PROCESSED_ARXIV_FILE.exists():
        try:
            data = json.loads(PROCESSED_ARXIV_FILE.read_text())
            return {"en": set(data.get("en", [])), "ru": set(data.get("ru", []))}
        except Exception as e:
            log.warning(f"processed_arxiv_papers.json: не удалось прочитать ({e}), начинаю с пустого")
    return {"en": set(), "ru": set()}


def _save_processed_arxiv():
    PROCESSED_ARXIV_FILE.write_text(json.dumps({
        "en": sorted(processed_arxiv_ids["en"]),
        "ru": sorted(processed_arxiv_ids["ru"]),
    }, indent=2))


def _load_processed_coindesk() -> set:
    if PROCESSED_COINDESK_FILE.exists():
        try:
            return set(json.loads(PROCESSED_COINDESK_FILE.read_text()))
        except Exception as e:
            log.warning(f"processed_coindesk_articles.json: не удалось прочитать ({e}), начинаю с пустого")
    return set()


def _save_processed_coindesk():
    PROCESSED_COINDESK_FILE.write_text(json.dumps(sorted(processed_coindesk_urls), indent=2))


def _load_processed_ru_auto() -> set:
    if PROCESSED_RU_AUTO_FILE.exists():
        try:
            return set(json.loads(PROCESSED_RU_AUTO_FILE.read_text()))
        except Exception as e:
            log.warning(f"processed_ru_auto_stories.json: не удалось прочитать ({e}), начинаю с пустого")
    return set()


def _save_processed_ru_auto():
    PROCESSED_RU_AUTO_FILE.write_text(json.dumps(sorted(processed_ru_auto_urls), indent=2))


def _load_processed_quarterly_digest() -> set:
    if PROCESSED_QUARTERLY_DIGEST_FILE.exists():
        try:
            return set(json.loads(PROCESSED_QUARTERLY_DIGEST_FILE.read_text()))
        except Exception as e:
            log.warning(f"processed_quarterly_digests.json: не удалось прочитать ({e}), начинаю с пустого")
    return set()


def _save_processed_quarterly_digest():
    PROCESSED_QUARTERLY_DIGEST_FILE.write_text(json.dumps(sorted(processed_quarterly_digest_urls), indent=2))


def _load_processed_jpmorgan() -> dict:
    if PROCESSED_JPMORGAN_FILE.exists():
        try:
            return json.loads(PROCESSED_JPMORGAN_FILE.read_text())
        except Exception as e:
            log.warning(f"processed_jpmorgan_brief.json: не удалось прочитать ({e}), начинаю с пустого")
    return {"marker": None}


def _save_processed_jpmorgan():
    PROCESSED_JPMORGAN_FILE.write_text(json.dumps(processed_jpmorgan_state, indent=2))


def _load_daily_publish_count() -> dict:
    if DAILY_PUBLISH_COUNT_FILE.exists():
        try:
            return json.loads(DAILY_PUBLISH_COUNT_FILE.read_text())
        except Exception as e:
            log.warning(f"daily_publish_count.json: не удалось прочитать ({e}), начинаю с пустого")
    return {}


def _save_daily_publish_count():
    DAILY_PUBLISH_COUNT_FILE.write_text(json.dumps(daily_publish_count, indent=2))


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _publishes_today(lang: str) -> int:
    return daily_publish_count.get(_today_key(), {}).get(lang, 0)


def _record_publish(lang: str):
    day = daily_publish_count.setdefault(_today_key(), {})
    day[lang] = day.get(lang, 0) + 1
    _save_daily_publish_count()


# Videos that were fully rendered but couldn't publish because the daily cap
# was already hit — retried automatically by _flush_deferred_publishes()
# instead of relying on the owner remembering to run /retry.
DEFERRED_PUBLISH_FILE = Path("deferred_publishes.json")
DEFERRED_FLUSH_INTERVAL_SECONDS = 30 * 60


def _load_deferred_publishes() -> list:
    if DEFERRED_PUBLISH_FILE.exists():
        try:
            return json.loads(DEFERRED_PUBLISH_FILE.read_text())
        except Exception as e:
            log.warning(f"deferred_publishes.json: не удалось прочитать ({e}), начинаю с пустого")
    return []


def _save_deferred_publishes():
    DEFERRED_PUBLISH_FILE.write_text(json.dumps(deferred_publishes, indent=2))


processed_coindesk_urls = _load_processed_coindesk()
processed_ru_auto_urls  = _load_processed_ru_auto()
processed_quarterly_digest_urls = _load_processed_quarterly_digest()
processed_jpmorgan_state = _load_processed_jpmorgan()   # {"marker": "<etag>|<last-modified>" | None}
daily_publish_count = _load_daily_publish_count()   # {"YYYY-MM-DD": {"ru": N, "en": N}}
deferred_publishes  = _load_deferred_publishes()    # [{chat_id, user_id, base_job_id, lang, category, video_path, thumb_path, vk_only}]

# Dedup for an auto-source is only committed once its job actually renders
# successfully (in on_job_done), not the moment it's enqueued — otherwise a
# TTS/render failure marks the item "done" forever with no retry path.
# base_job_id -> zero-arg callable that commits that source's dedup state
# (add-to-set-and-save for URL-based sources, overwrite-and-save for a
# single-slot source like JPMorgan's always-same-URL weekly brief).
job_dedup_pending: dict[str, Callable[[], None]] = {}


def _dedup_commit(item_set: set, item: str, save_fn: Callable[[], None]) -> Callable[[], None]:
    """Binds item_set/item/save_fn as this call's own values (not a shared
    loop variable) so a commit registered inside a loop — e.g. one per arxiv
    paper — fires for the paper it was actually created for."""
    def _commit():
        item_set.add(item)
        save_fn()
    return _commit


processed_arxiv_ids = _load_processed_arxiv()  # persists across restarts

AUTOMOTIVE_CATEGORY = "Auto Detail"
job_category: dict[str, str] = {}      # base_job_id (timestamp) → classified category
# base_job_id → description text lifted straight from an arxiv channel post,
# used instead of a fresh Gemini-generated one when the job came from there.
job_channel_description: dict[str, str] = {}
last_pdf: dict[int, tuple] = {}        # user_id → (pdf_path, job_dir, category) of the MOST RECENT PDF, for /retry
last_base_job_id: dict[int, str] = {}  # user_id → base_job_id of the MOST RECENT PDF, for /retry
# f"{base_job_id}:{lang}" → {video_path, thumb_path, title, description, category, youtube, vk}
# Keyed per-PDF (not just per-user!) so results from different PDFs sent by the same
# user never get mixed up — e.g. video #2's YouTube upload getting skipped because
# video #1's "already published" flag was sitting in the same slot.
last_result: dict[str, dict] = {}
pending_oauth: dict[int, str] = {}  # user_id → lang ("ru"/"en"), awaiting a pasted auth code

# ── Gemini / публикация ─────────────────────────────────────────────────────
gemini_ru = GeminiContentGenerator(config_ru2.CONFIG["gemini"]["api_key"], config_ru2.CONFIG["gemini"]["model"])
gemini_en = GeminiContentGenerator(config_en.CONFIG["gemini"]["api_key"], config_en.CONFIG["gemini"]["model"])

yt_ru = yt_en = vk_uploader = None

if config_ru2.CONFIG["youtube"]["auto_upload"] and os.path.exists(config_ru2.CONFIG["youtube"]["client_secrets_file"]):
    try:
        yt_ru = YouTubeUploader(
            config_ru2.CONFIG["youtube"]["client_secrets_file"],
            token_file="youtube_token_ru.pickle",
            oauth_ports=(8080, 8081),
            category_id=config_ru2.CONFIG["youtube"]["category_id"],
            privacy_status=config_ru2.CONFIG["youtube"]["privacy_status"],
        )
    except Exception as e:
        log.warning(f"⚠️ YouTube RU uploader не инициализирован: {e}")

if config_en.CONFIG["youtube"]["auto_upload"] and os.path.exists(config_en.CONFIG["youtube"]["client_secrets_file"]):
    try:
        yt_en = YouTubeUploader(
            config_en.CONFIG["youtube"]["client_secrets_file"],
            token_file="youtube_token_en.pickle",
            oauth_ports=(8080, 8081),
            category_id=config_en.CONFIG["youtube"]["category_id"],
            privacy_status=config_en.CONFIG["youtube"]["privacy_status"],
        )
    except Exception as e:
        log.warning(f"⚠️ YouTube EN uploader не инициализирован: {e}")

if config_ru2.CONFIG.get("vk", {}).get("access_token"):
    try:
        vk_uploader = VKUploader(config_ru2.CONFIG["vk"]["access_token"])
    except Exception as e:
        log.warning(f"⚠️ VK uploader не инициализирован: {e}")


# ── Хелперы ─────────────────────────────────────────────────────────────────
def make_progress_tracker(chat_id: int):
    """
    Returns a progress_fn bound to one job: the first call sends a status
    message, every call after that edits the SAME message in place instead
    of sending a new one — so a 6-step pipeline run produces one message
    that updates live, not a dozen separate lines in the chat.
    """
    state = {"message_id": None}

    async def progress(_chat_id: int, text: str):
        if state["message_id"] is None:
            msg = await client.send_message(chat_id, text)
            state["message_id"] = msg.id
            return
        try:
            await client.edit_message(chat_id, state["message_id"], text)
        except Exception as e:
            log.warning(f"progress edit failed ({e}), sending a new message instead")
            msg = await client.send_message(chat_id, text)
            state["message_id"] = msg.id

    return progress


def is_allowed(user_id: int) -> bool:
    if ALLOWED_USERS is None:
        return True
    return user_id in ALLOWED_USERS


# ── Обработчик входящих сообщений ───────────────────────────────────────────
@client.on(events.NewMessage(incoming=True, from_users=[OWNER_ID]))
async def handle_message(event):
    sender  = await event.get_sender()
    user_id = sender.id

    if not is_allowed(user_id):
        await event.reply("⛔ Нет доступа.")
        return

    # /start /help
    if event.text and event.text.strip() in ("/start", "/help"):
        await event.reply(
            "👋 Привет! Просто отправь PDF-файл статьи.\n\n"
            f"Также слежу за @{ARXIV_CHANNEL_EN} (EN) и @{ARXIV_CHANNEL_RU} (RU) — "
            "новый пост там сам скачивает PDF и запускает генерацию на своём языке.\n\n"
            "Дальше всё автоматически:\n"
            "🏷 Определяю тему\n"
            "🌍 Авто-детейлинг → только RU (VK+YouTube-RU); остальное → RU+EN\n"
            "🎬 Генерирую видео с озвучкой\n"
            "📤 Публикую на YouTube/VK (приватно, как в конфиге)\n\n"
            "Команды:\n"
            "/status — позиция в очереди\n"
            "/retry ru | /retry en — повторить генерацию+публикацию языка без дублей\n"
            "/retry vk — повторить только публикацию в VK (без пересборки видео)\n"
            "/youtube_auth ru | /youtube_auth en — переавторизовать YouTube, если токен истёк "
            "(работает без браузера на сервере)"
        )
        return

    # /status
    if event.text and event.text.strip() == "/status":
        pos = job_queue.position(user_id)
        if pos == 0:
            await event.reply("✅ Нет активных задач.")
        else:
            await event.reply(f"⏳ Ваша задача в очереди: позиция {pos}")
        return

    # /retry ru | en | vk
    if event.text and event.text.strip().lower().startswith("/retry"):
        parts = event.text.strip().split()
        target = parts[1].lower() if len(parts) > 1 else ""
        if target not in ("ru", "en", "vk"):
            await event.reply(
                "Использование:\n"
                "/retry ru — повторить RU (генерация+публикация, если ещё не было)\n"
                "/retry en — повторить EN\n"
                "/retry vk — повторить только публикацию последнего RU-видео в VK"
            )
            return
        await _handle_retry(event, user_id, target)
        return

    # /youtube_auth ru | en — переавторизация без браузера на сервере (copy-paste OAuth)
    if event.text and event.text.strip().lower().startswith("/youtube_auth"):
        parts = event.text.strip().split()
        lang = parts[1].lower() if len(parts) > 1 else ""
        if lang not in ("ru", "en"):
            await event.reply("Использование: /youtube_auth ru  или  /youtube_auth en")
            return
        yt = yt_ru if lang == "ru" else yt_en
        if not yt:
            await event.reply(
                f"⚠️ YouTube {lang.upper()} uploader не инициализирован — "
                f"проверь client_secrets_file в конфиге."
            )
            return
        auth_url = yt.start_manual_authorization()
        pending_oauth[user_id] = lang
        await event.reply(
            f"🔑 Открой эту ссылку в ЛЮБОМ браузере (с телефона тоже подойдёт) и войди "
            f"в {lang.upper()}-аккаунт Google:\n\n{auth_url}\n\n"
            f"После согласия браузер попробует открыть localhost и покажет ошибку — "
            f"это нормально. Скопируй ссылку целиком из адресной строки (или просто код "
            f"после code=) и пришли следующим сообщением сюда."
        )
        return

    # Ответ с кодом авторизации (если ждём)
    if event.text and user_id in pending_oauth and not event.text.strip().startswith("/"):
        lang = pending_oauth.pop(user_id)
        yt = yt_ru if lang == "ru" else yt_en
        try:
            yt.complete_manual_authorization(event.text.strip())
            await event.reply(f"✅ YouTube {lang.upper()} авторизован! Токен сохранён.")
        except Exception as e:
            await event.reply(f"❌ Не удалось завершить авторизацию: {e}\n\nПопробуй /youtube_auth {lang} заново.")
        return

    # PDF файл
    if event.document:
        mime  = event.document.mime_type or ""
        fname = ""
        for attr in event.document.attributes:
            if hasattr(attr, "file_name"):
                fname = attr.file_name or ""

        if "pdf" not in mime.lower() and not fname.lower().endswith(".pdf"):
            await event.reply("❌ Пожалуйста, отправь PDF-файл.")
            return

        job_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")  # microseconds: avoid
        job_dir = WORKSPACE / str(user_id) / job_id            # collisions across PDFs sent seconds apart
        job_dir.mkdir(parents=True, exist_ok=True)

        await event.reply("📥 Получил! Скачиваю PDF...")
        pdf_path = job_dir / "input.pdf"
        await client.download_media(event.document, file=str(pdf_path))

        await process_pdf(event.chat_id, user_id, pdf_path, job_dir)
        return

    if event.text:
        await event.reply("🤔 Не понял. Отправь PDF-файл или /help")


# ── Автослежение за каналами @arxivpaper (EN) и @arxivpaperu (RU) ───────────
@client.on(events.NewMessage(incoming=True, chats=ARXIV_CHANNEL_EN))
async def handle_arxiv_channel_en(event):
    await _process_arxiv_channel_post(event, ARXIV_CHANNEL_EN, forced_langs=["en"])


@client.on(events.NewMessage(incoming=True, chats=ARXIV_CHANNEL_RU))
async def handle_arxiv_channel_ru(event):
    await _process_arxiv_channel_post(event, ARXIV_CHANNEL_RU, forced_langs=["ru"])


def _arxiv_id(url: str) -> str:
    """https://arxiv.org/pdf/2609.11109v1 -> '2609.11109' (version-stripped,
    so a new version of an already-covered paper doesn't count as new)."""
    m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", url)
    return m.group(1) if m else url


def _extract_papers(event) -> list[dict]:
    """
    Returns one {title, pdf_url, arxiv_id} entry per paper in the post — a
    single-paper "Paper of the Day" post has one, a "Top N of the Week/Month"
    digest has several. Pairs each arxiv.org/abs/ link with the very next
    arxiv.org/pdf/ link that shares its arxiv ID (that's the order every
    real post from this channel uses: title link immediately followed by
    its own "pdf" link before the next paper starts).
    """
    # Entity offsets are in UTF-16 code units, not Python string indices — every
    # emoji before an entity (📊, 📄, ...) is a surrogate pair in UTF-16 but a
    # single Python character, silently shifting every later offset. Telethon's
    # own add_surrogate/del_surrogate round-trip is the documented fix.
    raw = tl_helpers.add_surrogate(event.message.message)
    entities = sorted(event.message.entities or [], key=lambda e: e.offset)
    papers = []
    pending_title = pending_id = None
    for entity in entities:
        url = getattr(entity, "url", None)
        if not url:
            continue
        if "arxiv.org/abs/" in url:
            pending_title = tl_helpers.del_surrogate(raw[entity.offset:entity.offset + entity.length])
            pending_id = _arxiv_id(url)
        elif "arxiv.org/pdf/" in url and pending_title and _arxiv_id(url) == pending_id:
            papers.append({"title": pending_title, "pdf_url": url, "arxiv_id": pending_id})
            pending_title = pending_id = None
    return papers


def _extract_channel_description(raw_text: str, title: str) -> str:
    """
    A single-paper "Paper of the Day" post looks like:
        <bold header>
        <title>                          (as a link)
        [RU channel repeats the title again in plain text here]
        <description paragraph>
        📊 <score line>
        📄 <download link>
        🎙️ <discussion link>
    Strips the header/title line(s) and the score+links footer, keeping only
    the description paragraph itself. (Multi-paper digests carry no
    per-paper abstract at all, so this is only called for single-paper posts.)
    """
    body = raw_text.split("\n📊")[0]           # drop the score line + trailing links
    lines = body.split("\n")[1:]               # drop the bold header line
    lines = [l for l in lines if l.strip().lower() != title.strip().lower()]  # drop duplicated title line(s)
    return "\n".join(lines).strip()


async def _process_arxiv_channel_post(event, channel_name: str, forced_langs: list):
    lang_key = forced_langs[0]  # each channel handler passes exactly one language
    papers = _extract_papers(event)
    if not papers:
        log.info(f"@{channel_name} пост {event.message.id}: PDF-ссылки не найдены, пропускаю")
        return

    for paper in papers:
        if paper["arxiv_id"] in processed_arxiv_ids[lang_key]:
            log.info(f"@{channel_name}: {paper['arxiv_id']} уже обработан для {lang_key}, пропускаю")
            continue

        suitable, reason = await asyncio.to_thread(is_suitable_pdf_url, paper["pdf_url"])
        if not suitable:
            log.warning(f"@{channel_name}: {paper['pdf_url']} не подходит ({reason})")
            continue

        # Only single-paper posts carry a real abstract paragraph to reuse.
        channel_description = (
            _extract_channel_description(event.message.message, paper["title"])
            if len(papers) == 1 else None
        )

        job_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        job_dir = WORKSPACE / str(OWNER_ID) / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        pdf_path = job_dir / "input.pdf"

        await client.send_message(
            OWNER_ID, f"📄 Новая статья из @{channel_name}: {paper['title']}\nСкачиваю: {paper['pdf_url']}")
        try:
            await asyncio.to_thread(download_pdf, paper["pdf_url"], pdf_path)
        except Exception as e:
            await client.send_message(OWNER_ID, f"❌ Не удалось скачать {paper['pdf_url']}: {e}")
            continue

        job_dedup_pending[job_id] = _dedup_commit(
            processed_arxiv_ids[lang_key], paper["arxiv_id"], _save_processed_arxiv)

        await process_pdf(OWNER_ID, OWNER_ID, pdf_path, job_dir,
                           forced_langs=forced_langs, channel_description=channel_description)


# ── Общая обработка PDF (ручная отправка + каналы) ───────────────────────────
async def process_pdf(chat_id: int, user_id: int, pdf_path: Path, job_dir: Path,
                       forced_langs: list = None, channel_description: str = None):
    job_id = job_dir.name

    await client.send_message(chat_id, "🔎 Определяю тему и куда публиковать...")
    blocks = await asyncio.to_thread(extract_pdf, pdf_path, job_dir / "sample")
    sample_text = "\n\n".join(b["text"] for b in blocks[:5])
    category = await asyncio.to_thread(
        gemini_ru.classify_topic, pdf_path.stem, sample_text, list(config_ru2.CONFIG["playlists"].keys()))
    if not category:
        category = "Other"
        log.warning(f"job {job_id}: classification failed, category='Other'")

    # A channel post already knows its own language (forced_langs); only the
    # manual-DM path derives ru/en from topic (automotive -> ru-only).
    langs = forced_langs if forced_langs is not None else (
        ["ru"] if category == AUTOMOTIVE_CATEGORY else ["ru", "en"])
    job_category[job_id] = category
    if channel_description:
        job_channel_description[job_id] = channel_description
    last_pdf[user_id] = (pdf_path, job_dir, category)
    last_base_job_id[user_id] = job_id

    await client.send_message(chat_id, f"🏷 Тема: «{category}» → {'/'.join(l.upper() for l in langs)}")
    await _enqueue_langs(chat_id, user_id, pdf_path, job_dir, langs)


async def _enqueue_langs(chat_id: int, user_id: int, pdf_path: Path, job_dir: Path, langs: list):
    for lang in langs:
        lang_dir = job_dir / lang
        lang_dir.mkdir(exist_ok=True)
        lang_pdf = lang_dir / "input.pdf"
        await asyncio.to_thread(shutil.copy2, pdf_path, lang_pdf)

        pos  = job_queue.queue_size() + 1
        flag = "🇷🇺" if lang == "ru" else "🇺🇸"

        if pos > 1:
            await client.send_message(chat_id, f"{flag} [{lang.upper()}] В очереди: позиция {pos}")
        else:
            await client.send_message(chat_id, f"{flag} [{lang.upper()}] Начинаю обработку!")

        job = Job(
            job_id=f"{job_dir.name}_{lang}",
            user_id=user_id,
            chat_id=chat_id,
            pdf_path=lang_pdf,
            job_dir=lang_dir,
            lang=lang,
            progress_fn=make_progress_tracker(chat_id),
        )
        await job_queue.enqueue(job)


async def _enqueue_langs_webpage(chat_id: int, user_id: int, blocks: list, job_dir: Path, langs: list):
    """Same as _enqueue_langs, but for a webpage source: no PDF to copy per
    language, the already-extracted blocks (text+images) are shared as-is —
    run_pipeline skips its own extraction step when pre_extracted_blocks is set."""
    for lang in langs:
        lang_dir = job_dir / lang
        lang_dir.mkdir(exist_ok=True)

        pos  = job_queue.queue_size() + 1
        flag = "🇷🇺" if lang == "ru" else "🇺🇸"

        if pos > 1:
            await client.send_message(chat_id, f"{flag} [{lang.upper()}] В очереди: позиция {pos}")
        else:
            await client.send_message(chat_id, f"{flag} [{lang.upper()}] Начинаю обработку!")

        job = Job(
            job_id=f"{job_dir.name}_{lang}",
            user_id=user_id,
            chat_id=chat_id,
            pdf_path=job_dir / "source_url.txt",  # unused: pre_extracted_blocks skips step 1
            job_dir=lang_dir,
            lang=lang,
            progress_fn=make_progress_tracker(chat_id),
            pre_extracted_blocks=blocks,
        )
        await job_queue.enqueue(job)


# ── Ежедневная проверка крипто-рассылок CoinDesk ─────────────────────────────
async def _process_coindesk_article(url: str):
    if url in processed_coindesk_urls:
        return

    job_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_dir = WORKSPACE / str(OWNER_ID) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    await client.send_message(OWNER_ID, f"📰 Новая статья CoinDesk, скачиваю: {url}")
    try:
        blocks = await asyncio.to_thread(extract_webpage, url, job_dir)
    except Exception as e:
        await client.send_message(OWNER_ID, f"❌ Не удалось обработать {url}: {e}")
        return

    sample_text = blocks[0]["text"][:3000]
    category = await asyncio.to_thread(
        gemini_ru.classify_topic, url.rsplit("/", 1)[-1], sample_text, list(config_ru2.CONFIG["playlists"].keys()))
    if not category:
        category = "Crypto Ideas"  # sane default given the source
        log.warning(f"coindesk {url}: classification failed, defaulting to 'Crypto Ideas'")

    langs = ["ru"] if category == AUTOMOTIVE_CATEGORY else ["ru", "en"]
    job_category[job_id] = category
    last_base_job_id[OWNER_ID] = job_id

    job_dedup_pending[job_id] = _dedup_commit(processed_coindesk_urls, url, _save_processed_coindesk)

    await client.send_message(OWNER_ID, f"🏷 Тема: «{category}» → {'/'.join(l.upper() for l in langs)}")
    await _enqueue_langs_webpage(OWNER_ID, OWNER_ID, blocks, job_dir, langs)


async def _check_coindesk_newsletters():
    while True:
        for newsletter_url in COINDESK_NEWSLETTERS:
            try:
                links = await asyncio.to_thread(find_article_links, newsletter_url, 5)
                for link in reversed(links):  # process oldest-of-the-batch first
                    if link not in processed_coindesk_urls:
                        await _process_coindesk_article(link)
            except Exception as e:
                log.error(f"CoinDesk: проверка {newsletter_url} не удалась: {e}")
        await asyncio.sleep(COINDESK_CHECK_INTERVAL_SECONDS)


# ── Ежедневный выбор одного авто-сюжета (за рулем / kolesa.ru / autonews.ru) ─
async def _process_ru_auto_story(url: str, title: str):
    if url in processed_ru_auto_urls:
        return

    job_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_dir = WORKSPACE / str(OWNER_ID) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    await client.send_message(OWNER_ID, f"🚗 Сюжет дня (авто): {title}\n{url}")
    try:
        blocks = await asyncio.to_thread(extract_webpage, url, job_dir)
    except Exception as e:
        await client.send_message(OWNER_ID, f"❌ Не удалось обработать {url}: {e}")
        return

    # Source is automotive by construction (all 3 sites are auto-only), so no
    # classification call is needed — routes RU-only per the existing rule.
    job_category[job_id] = AUTOMOTIVE_CATEGORY
    last_base_job_id[OWNER_ID] = job_id

    job_dedup_pending[job_id] = _dedup_commit(processed_ru_auto_urls, url, _save_processed_ru_auto)

    await client.send_message(OWNER_ID, f"🏷 Тема: «{AUTOMOTIVE_CATEGORY}» → RU")
    await _enqueue_langs_webpage(OWNER_ID, OWNER_ID, blocks, job_dir, ["ru"])


async def _check_ru_auto_daily():
    while True:
        try:
            links = await asyncio.to_thread(find_ru_auto_links, 5)
            new_links = [l for l in links if l not in processed_ru_auto_urls]

            candidates = []
            for url in new_links:
                try:
                    title = await asyncio.to_thread(extract_page_title, url)
                    candidates.append({"title": title, "url": url})
                except Exception as e:
                    log.warning(f"RU auto: title fetch failed for {url}: {e}")

            if candidates:
                idx = await asyncio.to_thread(gemini_ru.pick_most_interesting, candidates)
                chosen = candidates[idx]
                await _process_ru_auto_story(chosen["url"], chosen["title"])
        except Exception as e:
            log.error(f"RU auto: ежедневная проверка не удалась: {e}")
        await asyncio.sleep(RU_AUTO_CHECK_INTERVAL_SECONDS)


# ── Квартальный обзор импорта авто по всем странам-партнёрам ─────────────────
async def _process_quarterly_digest(url: str, title: str):
    if url in processed_quarterly_digest_urls:
        return

    job_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_dir = WORKSPACE / str(OWNER_ID) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    await client.send_message(OWNER_ID, f"📊 Квартальный обзор импорта авто: {title}\n{url}")
    try:
        blocks = await asyncio.to_thread(extract_webpage, url, job_dir)
    except Exception as e:
        await client.send_message(OWNER_ID, f"❌ Не удалось обработать {url}: {e}")
        return

    job_category[job_id] = AUTOMOTIVE_CATEGORY
    last_base_job_id[OWNER_ID] = job_id

    job_dedup_pending[job_id] = _dedup_commit(processed_quarterly_digest_urls, url, _save_processed_quarterly_digest)

    await client.send_message(OWNER_ID, f"🏷 Тема: «{AUTOMOTIVE_CATEGORY}» → RU")
    await _enqueue_langs_webpage(OWNER_ID, OWNER_ID, blocks, job_dir, ["ru"])


async def _check_quarterly_digest():
    while True:
        try:
            candidates = await asyncio.to_thread(find_quarterly_digest_candidates, 20)
            for c in candidates:
                if c["url"] not in processed_quarterly_digest_urls:
                    await _process_quarterly_digest(c["url"], c["title"])
        except Exception as e:
            log.error(f"Квартальный обзор: проверка не удалась: {e}")
        await asyncio.sleep(QUARTERLY_DIGEST_CHECK_INTERVAL_SECONDS)


# ── Еженедельный обзор рынка JPMorgan Asset Management ───────────────────────
async def _process_jpmorgan_brief(marker: str):
    job_id  = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    job_dir = WORKSPACE / str(OWNER_ID) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = job_dir / "input.pdf"

    await client.send_message(OWNER_ID, "📈 Новый еженедельный обзор рынка JPMorgan, скачиваю...")
    try:
        await asyncio.to_thread(download_pdf, JPMORGAN_WEEKLY_BRIEF_URL, pdf_path)
    except Exception as e:
        await client.send_message(OWNER_ID, f"❌ Не удалось скачать JPMorgan brief: {e}")
        return

    def _commit():
        processed_jpmorgan_state["marker"] = marker
        _save_processed_jpmorgan()
    job_dedup_pending[job_id] = _commit

    # Self-classifies via process_pdf (should land on "GPMorgan report
    # debates"), which also auto-splits ru+en since this is never automotive.
    await process_pdf(OWNER_ID, OWNER_ID, pdf_path, job_dir)


async def _check_jpmorgan_weekly():
    while True:
        try:
            marker = await asyncio.to_thread(get_pdf_change_marker, JPMORGAN_WEEKLY_BRIEF_URL)
            if marker and marker != processed_jpmorgan_state.get("marker"):
                await _process_jpmorgan_brief(marker)
        except Exception as e:
            log.error(f"JPMorgan: еженедельная проверка не удалась: {e}")
        await asyncio.sleep(JPMORGAN_CHECK_INTERVAL_SECONDS)


# ── Публикация (переиспользуется при обычном запуске и при /retry) ──────────
async def _publish(chat_id: int, user_id: int, base_job_id: str, lang: str, category: str,
                    video_path: Path, thumb_path: Path, vk_only: bool = False, force: bool = False):
    flag = "🇷🇺" if lang == "ru" else "🇺🇸"

    entry = last_result.setdefault(f"{base_job_id}:{lang}", {
        "video_path": None, "thumb_path": None, "title": None, "description": None,
        "category": None, "youtube": None, "vk": None,
    })
    entry["video_path"], entry["thumb_path"], entry["category"] = video_path, thumb_path, category

    if entry["title"] is None:
        title, key_points = load_script_title_and_points(video_path.parent)
        channel_desc = job_channel_description.get(base_job_id)
        if channel_desc:
            entry["description"] = append_footer(channel_desc, lang)
        else:
            gemini = gemini_ru if lang == "ru" else gemini_en
            entry["description"] = await asyncio.to_thread(gemini.generate_description, title, key_points, lang=lang)
        entry["title"] = title
    title, description = entry["title"], entry["description"]

    if lang == "ru":
        yt = yt_ru
        playlist_id = config_ru2.CONFIG["playlists"].get(category)
        vk_owner_id = config_ru2.CONFIG["vk"]["channels"].get(category)
    else:
        yt = yt_en
        playlist_id = config_en.CONFIG["playlists"].get(category)
        vk_owner_id = None

    lines = []
    youtube_url = None
    did_new_upload = False   # only true once something actually succeeds — a
                              # failed attempt must not eat into the daily cap
    deferred = False
    cap_hit = not force and _publishes_today(lang) >= DAILY_VIDEO_CAP

    if not vk_only:
        if entry["youtube"] and entry["youtube"].get("success"):
            youtube_url = entry["youtube"]["url"]
            lines.append(f"▶️ YouTube: {youtube_url} (уже опубликовано)")
        elif cap_hit:
            deferred = True
            lines.append(f"⏸ YouTube: дневной лимит ({DAILY_VIDEO_CAP}) на сегодня достигнут — опубликую автоматически позже")
        elif yt and yt.is_authorized:
            yt_result = await asyncio.to_thread(
                yt.upload_video, str(video_path), title, description,
                thumbnail_path=str(thumb_path), playlist_id=playlist_id)
            entry["youtube"] = yt_result
            if yt_result.get("success"):
                youtube_url = yt_result["url"]
                did_new_upload = True
                lines.append(f"▶️ YouTube: {youtube_url}")
            else:
                lines.append(f"⚠️ YouTube: {yt_result.get('error')}")
        elif yt:
            lines.append(f"⚠️ YouTube {lang.upper()} не авторизован — используй /youtube_auth {lang}")
        else:
            lines.append("⚠️ YouTube uploader не настроен")

    if lang == "ru" and vk_uploader and vk_owner_id:
        if entry["vk"] and entry["vk"].get("success"):
            lines.append(f"📹 VK: {entry['vk']['url']} (уже опубликовано)")
        elif cap_hit:
            deferred = True
            lines.append(f"⏸ VK: дневной лимит ({DAILY_VIDEO_CAP}) на сегодня достигнут — опубликую автоматически позже")
        else:
            vk_result = await asyncio.to_thread(
                vk_uploader.upload_video, str(video_path), title, description,
                owner_id=vk_owner_id, thumbnail_path=str(thumb_path))
            entry["vk"] = vk_result
            if vk_result.get("success"):
                did_new_upload = True
                lines.append(f"📹 VK: {vk_result['url']}")
            else:
                lines.append(f"⚠️ VK: {vk_result.get('error')}")

    if did_new_upload:
        _record_publish(lang)

    await client.send_file(
        chat_id,
        str(thumb_path),
        caption=f"{flag} {title}\n📂 {category}\n\n" + "\n".join(lines),
        force_document=False,
    )

    if lang == "en" and youtube_url:
        patreon_text = format_patreon_post(title, description, youtube_url)
        await client.send_message(
            chat_id,
            "📋 Готово для Patreon (нажми на блок, чтобы скопировать):\n\n"
            f"```\n{patreon_text}\n```",
            parse_mode="markdown",
        )

    return not deferred


async def _handle_retry(event, user_id: int, target: str):
    base_job_id = last_base_job_id.get(user_id)  # most recently received PDF's job

    if target == "vk":
        entry = last_result.get(f"{base_job_id}:ru") if base_job_id else None
        if not entry or not entry.get("video_path"):
            await event.reply("⚠️ Нет готового RU-видео для повторной публикации в VK. Отправь PDF заново.")
            return
        await event.reply("🔁 Повторяю публикацию в VK (в обход дневного лимита)...")
        await _publish(event.chat_id, user_id, base_job_id, "ru", entry["category"],
                        entry["video_path"], entry["thumb_path"], vk_only=True, force=True)
        return

    lang = target
    entry = last_result.get(f"{base_job_id}:{lang}") if base_job_id else None
    if entry and entry.get("video_path"):
        await event.reply(f"🔁 [{lang.upper()}] Видео уже готово, повторяю публикацию (в обход дневного лимита)...")
        await _publish(event.chat_id, user_id, base_job_id, lang, entry["category"],
                        entry["video_path"], entry["thumb_path"], force=True)
        return

    if user_id not in last_pdf:
        await event.reply("⚠️ Нет сохранённого PDF для повтора. Отправь файл заново.")
        return
    pdf_path, job_dir, category = last_pdf[user_id]
    job_category[job_dir.name] = category
    await event.reply(f"🔁 [{lang.upper()}] Начинаю обработку заново...")
    await _enqueue_langs(event.chat_id, user_id, pdf_path, job_dir, [lang])


# ── Колбэк завершения задачи ─────────────────────────────────────────────────
def _register_deferred(chat_id, user_id, base_job_id, lang, category, video_path, thumb_path, vk_only):
    key = f"{base_job_id}:{lang}:{vk_only}"
    if any(f"{d['base_job_id']}:{d['lang']}:{d['vk_only']}" == key for d in deferred_publishes):
        return
    deferred_publishes.append({
        "chat_id": chat_id, "user_id": user_id, "base_job_id": base_job_id, "lang": lang,
        "category": category, "video_path": str(video_path), "thumb_path": str(thumb_path),
        "vk_only": vk_only,
    })
    _save_deferred_publishes()


async def on_job_done(job: "Job", video_path, thumb_path, error):
    flag = "🇷🇺" if job.lang == "ru" else "🇺🇸"
    base_job_id = job.job_dir.parent.name

    if error:
        await client.send_message(
            job.chat_id,
            f"❌ {flag} Ошибка:\n`{error}`\n\n"
            f"/retry {job.lang} — повторить только этот язык, без пересборки другого"
        )
        return

    # Rendering succeeded — only now is it safe to mark the source item as
    # "done" so a render failure doesn't permanently skip it.
    commit_dedup = job_dedup_pending.pop(base_job_id, None)
    if commit_dedup:
        commit_dedup()

    category = job_category.get(base_job_id, "Other")

    await client.send_message(job.chat_id, f"{flag} Видео готово, публикую...")
    fully_published = await _publish(job.chat_id, job.user_id, base_job_id, job.lang, category, video_path, thumb_path)
    if not fully_published:
        _register_deferred(job.chat_id, job.user_id, base_job_id, job.lang, category, video_path, thumb_path, vk_only=False)


async def _flush_deferred_publishes():
    while True:
        await asyncio.sleep(DEFERRED_FLUSH_INTERVAL_SECONDS)
        for entry in list(deferred_publishes):
            try:
                fully_published = await _publish(
                    entry["chat_id"], entry["user_id"], entry["base_job_id"], entry["lang"],
                    entry["category"], Path(entry["video_path"]), Path(entry["thumb_path"]),
                    vk_only=entry["vk_only"])
            except Exception as e:
                log.error(f"Отложенная публикация {entry['base_job_id']}:{entry['lang']} не удалась: {e}")
                continue
            if fully_published:
                deferred_publishes.remove(entry)
                _save_deferred_publishes()


async def _ensure_joined(channel_username: str):
    """
    events.NewMessage only delivers real-time updates for channels the
    account has actually joined — get_messages()/get_entity() work for any
    public channel by username regardless of membership, which is why this
    can silently look fine in testing while the live listener never fires.
    Idempotent: joining an already-joined channel is a harmless no-op.
    """
    try:
        entity = await client.get_entity(channel_username)
        await client(JoinChannelRequest(entity))
        log.info(f"@{channel_username}: подписка подтверждена")
    except Exception as e:
        log.warning(f"@{channel_username}: не удалось подписаться ({e})")


# ── Старт ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Запуск NotebookLM userbot...")
    await client.start(phone=PHONE_NUMBER)
    log.info("Клиент подключён. Ожидаем сообщения...")
    await _ensure_joined(ARXIV_CHANNEL_EN)
    await _ensure_joined(ARXIV_CHANNEL_RU)
    asyncio.create_task(job_queue.run(on_done_callback=on_job_done))
    asyncio.create_task(_check_coindesk_newsletters())
    asyncio.create_task(_check_ru_auto_daily())
    asyncio.create_task(_check_quarterly_digest())
    asyncio.create_task(_check_jpmorgan_weekly())
    asyncio.create_task(_flush_deferred_publishes())
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())