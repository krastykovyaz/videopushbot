"""
NotebookLM Clone — Telethon userbot
Принимает PDF в личку, автоматически определяет тему и язык(и),
генерирует видео и публикует его на YouTube/VK без ручных шагов.
"""

import asyncio
import os
import shutil
import logging
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
from telethon import TelegramClient, events

import config_en
import config_ru2
from common.gemini_client import GeminiContentGenerator
from common.metadata import load_script_title_and_points
from common.vk_uploader import VKUploader
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

AUTOMOTIVE_CATEGORY = "Auto Detail"
job_category: dict[str, str] = {}      # base_job_id (timestamp) → classified category
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
            oauth_ports=(8082, 8083, 8084),
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
async def progress(chat_id: int, text: str):
    await client.send_message(chat_id, text)


def is_allowed(user_id: int) -> bool:
    if ALLOWED_USERS is None:
        return True
    return user_id in ALLOWED_USERS


# ── Обработчик входящих сообщений ───────────────────────────────────────────
@client.on(events.NewMessage(incoming=True, from_users=[8591956842]))
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

        await event.reply("🔎 Определяю тему и куда публиковать...")
        blocks = await asyncio.to_thread(extract_pdf, pdf_path, job_dir / "sample")
        sample_text = "\n\n".join(b["text"] for b in blocks[:5])
        category = gemini_ru.classify_topic(
            pdf_path.stem, sample_text, list(config_ru2.CONFIG["playlists"].keys()))
        if not category:
            category = "Other"
            log.warning(f"job {job_id}: classification failed, category='Other'")

        langs = ["ru"] if category == AUTOMOTIVE_CATEGORY else ["ru", "en"]
        job_category[job_id] = category
        last_pdf[user_id] = (pdf_path, job_dir, category)
        last_base_job_id[user_id] = job_id

        await event.reply(f"🏷 Тема: «{category}» → {'/'.join(l.upper() for l in langs)}")
        await _enqueue_langs(event, user_id, pdf_path, job_dir, langs)
        return

    if event.text:
        await event.reply("🤔 Не понял. Отправь PDF-файл или /help")


async def _enqueue_langs(event, user_id: int, pdf_path: Path, job_dir: Path, langs: list):
    for lang in langs:
        lang_dir = job_dir / lang
        lang_dir.mkdir(exist_ok=True)
        lang_pdf = lang_dir / "input.pdf"
        shutil.copy2(pdf_path, lang_pdf)

        pos  = job_queue.queue_size() + 1
        flag = "🇷🇺" if lang == "ru" else "🇺🇸"

        if pos > 1:
            await event.reply(f"{flag} [{lang.upper()}] В очереди: позиция {pos}")
        else:
            await event.reply(f"{flag} [{lang.upper()}] Начинаю обработку!")

        job = Job(
            job_id=f"{job_dir.name}_{lang}",
            user_id=user_id,
            chat_id=event.chat_id,
            pdf_path=lang_pdf,
            job_dir=lang_dir,
            lang=lang,
            progress_fn=progress,
        )
        await job_queue.enqueue(job)


# ── Публикация (переиспользуется при обычном запуске и при /retry) ──────────
async def _publish(chat_id: int, user_id: int, base_job_id: str, lang: str, category: str,
                    video_path: Path, thumb_path: Path, vk_only: bool = False):
    flag = "🇷🇺" if lang == "ru" else "🇺🇸"

    entry = last_result.setdefault(f"{base_job_id}:{lang}", {
        "video_path": None, "thumb_path": None, "title": None, "description": None,
        "category": None, "youtube": None, "vk": None,
    })
    entry["video_path"], entry["thumb_path"], entry["category"] = video_path, thumb_path, category

    if entry["title"] is None:
        title, key_points = load_script_title_and_points(video_path.parent)
        gemini = gemini_ru if lang == "ru" else gemini_en
        entry["description"] = gemini.generate_description(title, key_points, lang=lang)
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

    if not vk_only:
        if entry["youtube"] and entry["youtube"].get("success"):
            lines.append(f"▶️ YouTube: {entry['youtube']['url']} (уже опубликовано)")
        elif yt and yt.is_authorized:
            yt_result = await asyncio.to_thread(
                yt.upload_video, str(video_path), title, description,
                thumbnail_path=str(thumb_path), playlist_id=playlist_id)
            entry["youtube"] = yt_result
            lines.append(f"▶️ YouTube: {yt_result['url']}" if yt_result.get("success")
                          else f"⚠️ YouTube: {yt_result.get('error')}")
        elif yt:
            lines.append(f"⚠️ YouTube {lang.upper()} не авторизован — используй /youtube_auth {lang}")
        else:
            lines.append("⚠️ YouTube uploader не настроен")

    if lang == "ru" and vk_uploader and vk_owner_id:
        vk_result = await asyncio.to_thread(
            vk_uploader.upload_video, str(video_path), title, description,
            owner_id=vk_owner_id, thumbnail_path=str(thumb_path))
        entry["vk"] = vk_result
        lines.append(f"📹 VK: {vk_result['url']}" if vk_result.get("success")
                      else f"⚠️ VK: {vk_result.get('error')}")

    await client.send_file(
        chat_id,
        str(thumb_path),
        caption=f"{flag} {title}\n📂 {category}\n\n" + "\n".join(lines),
        force_document=False,
    )


async def _handle_retry(event, user_id: int, target: str):
    base_job_id = last_base_job_id.get(user_id)  # most recently received PDF's job

    if target == "vk":
        entry = last_result.get(f"{base_job_id}:ru") if base_job_id else None
        if not entry or not entry.get("video_path"):
            await event.reply("⚠️ Нет готового RU-видео для повторной публикации в VK. Отправь PDF заново.")
            return
        await event.reply("🔁 Повторяю публикацию в VK...")
        await _publish(event.chat_id, user_id, base_job_id, "ru", entry["category"],
                        entry["video_path"], entry["thumb_path"], vk_only=True)
        return

    lang = target
    entry = last_result.get(f"{base_job_id}:{lang}") if base_job_id else None
    if entry and entry.get("video_path"):
        await event.reply(f"🔁 [{lang.upper()}] Видео уже готово, повторяю публикацию...")
        await _publish(event.chat_id, user_id, base_job_id, lang, entry["category"],
                        entry["video_path"], entry["thumb_path"])
        return

    if user_id not in last_pdf:
        await event.reply("⚠️ Нет сохранённого PDF для повтора. Отправь файл заново.")
        return
    pdf_path, job_dir, category = last_pdf[user_id]
    job_category[job_dir.name] = category
    await event.reply(f"🔁 [{lang.upper()}] Начинаю обработку заново...")
    await _enqueue_langs(event, user_id, pdf_path, job_dir, [lang])


# ── Колбэк завершения задачи ─────────────────────────────────────────────────
async def on_job_done(job: "Job", video_path, thumb_path, error):
    flag = "🇷🇺" if job.lang == "ru" else "🇺🇸"

    if error:
        await client.send_message(
            job.chat_id,
            f"❌ {flag} Ошибка:\n`{error}`\n\n"
            f"/retry {job.lang} — повторить только этот язык, без пересборки другого"
        )
        return

    base_job_id = job.job_dir.parent.name
    category = job_category.get(base_job_id, "Other")

    await client.send_message(job.chat_id, f"{flag} Видео готово, публикую...")
    await _publish(job.chat_id, job.user_id, base_job_id, job.lang, category, video_path, thumb_path)


# ── Старт ────────────────────────────────────────────────────────────────────
async def main():
    log.info("Запуск NotebookLM userbot...")
    await client.start(phone=PHONE_NUMBER)
    log.info("Клиент подключён. Ожидаем сообщения...")
    asyncio.create_task(job_queue.run(on_done_callback=on_job_done))
    await client.run_until_disconnected()

if __name__ == "__main__":
    asyncio.run(main())