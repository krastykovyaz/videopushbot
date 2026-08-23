"""
Telegram бот для приёма файлов (до 2GB) с автоматической генерацией metadata
Использует pyrogram + Gemini AI + YouTube API + VK API
"""

import os
import json
import logging
import time
import requests
import asyncio
import PyPDF2

from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message
from config_ru2 import CONFIG
from common.gemini_client import GeminiContentGenerator
from common.youtube_uploader import YouTubeUploader
from common.vk_uploader import VKUploader

# =============================================================================
# ЛОГИРОВАНИЕ
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('telegram_bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# =============================================================================
# GEMINI GENERATOR
# =============================================================================

gemini_gen = GeminiContentGenerator(CONFIG['gemini']['api_key'], CONFIG['gemini']['model'])

# =============================================================================
# ИНИЦИАЛИЗАЦИЯ UPLOADERS
# =============================================================================

youtube_uploader = None
youtube_init_error = None
if CONFIG['youtube']['auto_upload'] and os.path.exists(CONFIG['youtube']['client_secrets_file']):
    try:
        youtube_uploader = YouTubeUploader(
            CONFIG['youtube']['client_secrets_file'],
            token_file='youtube_token_ru.pickle',
            oauth_ports=(8082, 8083, 8084),
            category_id=CONFIG['youtube']['category_id'],
            privacy_status=CONFIG['youtube']['privacy_status'],
        )
    except Exception as e:
        youtube_init_error = str(e)
        logging.warning(f"⚠️  YouTube не инициализирован: {e}")

vk_uploader = None
if CONFIG.get('vk', {}).get('access_token'):
    try:
        vk_uploader = VKUploader(CONFIG['vk']['access_token'])
    except Exception as e:
        logging.warning(f"⚠️  VK не инициализирован: {e}")

# =============================================================================
# TELEGRAM CLIENT
# =============================================================================

app = Client(
    "session_machiavelli",
    api_id=CONFIG['api_id'],
    api_hash=CONFIG['api_hash'],
    phone_number=CONFIG['phone_number']
)

user_states = {}
pending_video_metadata_by_user = {}

# Фильтр — только личный чат с владельцем
OWNER_ID = 8591956842
owner_filter = filters.create(lambda _, __, m: (
    m is not None
    and m.from_user is not None
    and m.from_user.id == OWNER_ID
    and m.chat is not None
    and m.chat.id == OWNER_ID  # личный чат: chat_id == user_id
))

# =============================================================================
# УТИЛИТЫ
# =============================================================================

def is_allowed_user(user_id):
    if not CONFIG['allowed_users']:
        return True
    return user_id in CONFIG['allowed_users']

def extract_user_id(message):
    if not message or not message.from_user:
        return None
    return message.from_user.id

def is_video_file(filename):
    return os.path.splitext(filename)[1].lower() in CONFIG['video_extensions']

def is_document_file(filename):
    return os.path.splitext(filename)[1].lower() in CONFIG['document_extensions']

def read_pdf(file_path):
    try:
        with open(file_path, 'rb') as f:
            pdf = PyPDF2.PdfReader(f)
            title = pdf.metadata.title if pdf.metadata and pdf.metadata.title else ""
            text = ""
            for page in pdf.pages[:5]:
                text += page.extract_text()
            if not title and text:
                title = ' '.join(text.split('\n')[:3]).strip()[:100]
            return title, text
    except Exception as e:
        logging.error(f"PDF read error: {e}")
        return "", ""

def read_text(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        lines = content.split('\n')
        title = next((l.strip().lstrip('#').strip() for l in lines if l.strip()), "")
        return title, content
    except Exception as e:
        logging.error(f"Text read error: {e}")
        return "", ""

def read_document(file_path):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        return read_pdf(file_path)
    elif ext in ['.txt', '.md']:
        return read_text(file_path)
    return "", ""

def cleanup_stale_json(current_base):
    folder = CONFIG['download_folder']
    if not os.path.exists(folder):
        return
    for name in os.listdir(folder):
        if name.endswith('.json') and name != f"{current_base}.json":
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                os.remove(path)
                logging.info(f"🧹 Удалён старый json: {name}")

# =============================================================================
# КОМАНДЫ
# =============================================================================

@app.on_message(owner_filter & filters.command("start"))
async def start_command(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return
    await message.reply_text(
        "👋 **Telegram File Bot + AI**\n\n"
        "📄 Отправьте PDF/TXT → бот сгенерирует metadata\n"
        "🎬 Отправьте видео → загрузится на YouTube и/или ВК\n\n"
        "/help — справка\n/stats — статистика"
    )

@app.on_message(owner_filter & filters.command("help"))
async def help_command(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return
    yt_status = "✅ готов" if youtube_uploader else f"❌ {youtube_init_error or 'не инициализирован'}"
    vk_status = "✅ готов" if vk_uploader else "❌ не инициализирован"
    await message.reply_text(
        f"📖 **Инструкция:**\n\n"
        f"1. Отправьте PDF/TXT\n"
        f"2. Ответьте на вопросы (ссылка → плейлист → платформа → thumbnail)\n"
        f"3. Отправьте видео → автозагрузка\n\n"
        f"**Статус:**\n"
        f"YouTube: {yt_status}\n"
        f"ВКонтакте: {vk_status}\n\n"
        f"**Ваш ID:** `{user_id}`"
    )

@app.on_message(owner_filter & filters.command("stats"))
async def stats_command(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return
    folder = CONFIG['download_folder']
    if not os.path.exists(folder):
        await message.reply_text("📂 Папка пустая")
        return
    files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    total = sum(os.path.getsize(os.path.join(folder, f)) for f in files)
    await message.reply_text(
        f"📊 **Статистика:**\n\n"
        f"📁 Файлов: {len(files)}\n"
        f"💾 Размер: {total / (1024**2):.2f} MB\n"
        f"📂 Папка: `{folder}`"
    )

# =============================================================================
# ОБРАБОТЧИК ДОКУМЕНТОВ
# =============================================================================

@app.on_message(owner_filter & filters.document)
async def handle_document(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return

    doc = message.document
    file_name = doc.file_name or f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    file_size = doc.file_size

    if not is_document_file(file_name) and not is_video_file(file_name):
        await message.reply_text(
            f"❌ Неподдерживаемый формат!\n"
            f"Видео: {', '.join(CONFIG['video_extensions'])}\n"
            f"Документы: {', '.join(CONFIG['document_extensions'])}"
        )
        return

    if file_size > CONFIG['max_file_size']:
        await message.reply_text(f"❌ Файл > {CONFIG['max_file_size'] / (1024**3):.1f} GB")
        return

    os.makedirs(CONFIG['download_folder'], exist_ok=True)
    file_path = os.path.join(CONFIG['download_folder'], file_name)
    if os.path.exists(file_path):
        base, ext = os.path.splitext(file_name)
        file_name = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        file_path = os.path.join(CONFIG['download_folder'], file_name)

    status = await message.reply_text(f"📥 Загрузка...\n📄 {file_name}\n💾 {file_size / (1024**2):.2f} MB")

    try:
        await message.download(file_name=file_path)

        if is_document_file(file_name):
            await status.edit_text("📄 Загружен! Читаю документ...")
            title, content = read_document(file_path)

            if not title or not content:
                await status.edit_text("❌ Не удалось прочитать документ")
                return

            title = title.replace('\n', ' ').strip()[:100]
            await status.edit_text(f"🤖 Генерирую описание через Gemini...\n\n📌 {title}")

            description = gemini_gen.generate_description(title, content, lang="ru")

            user_states[user_id] = {
                'file_name': file_name,
                'file_path': file_path,
                'title': title,
                'description': description,
                'waiting_for': 'link',
                'step': 1
            }

            await status.edit_text(
                f"✅ Описание готово!\n\n"
                f"📌 **Заголовок:**\n{title}\n\n"
                f"📝 **Описание:**\n{description[:200]}...\n\n"
                f"🔗 **Шаг 1/4:** Ссылка на источник\n(или `-` если нет)"
            )
        else:
            await status.edit_text(f"✅ Файл загружен!\n\n📄 {file_name}")
            logging.info(f"✅ File saved: {file_path}")

    except Exception as e:
        await status.edit_text(f"❌ Ошибка: `{e}`")
        logging.error(f"Document error: {e}")

# =============================================================================
# ОБРАБОТЧИК ВИДЕО
# =============================================================================

@app.on_message(owner_filter & filters.video)
async def handle_video(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return

    video = message.video
    original_name = video.file_name or f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    file_size = video.file_size

    if file_size > CONFIG['max_file_size']:
        await message.reply_text(f"❌ Файл слишком большой: {file_size / (1024**3):.2f} GB")
        return

    os.makedirs(CONFIG['download_folder'], exist_ok=True)
    original_base, ext = os.path.splitext(original_name)

    # Ищем подходящий json
    selected_base = None
    exact_json = os.path.join(CONFIG['download_folder'], f"{original_base}.json")
    if os.path.exists(exact_json):
        selected_base = original_base
    else:
        pending = pending_video_metadata_by_user.get(user_id)
        if pending:
            pending_json = os.path.join(CONFIG['download_folder'], f"{pending}.json")
            if os.path.exists(pending_json):
                selected_base = pending

    if not selected_base:
        await message.reply_text(
            "❌ Metadata не найден.\n\n"
            "Сначала отправьте PDF/TXT и пройдите диалог создания metadata."
        )
        return

    new_name = f"{selected_base}{ext}"
    file_path = os.path.join(CONFIG['download_folder'], new_name)

    status = await message.reply_text(
        f"📥 Загрузка видео...\n"
        f"📄 {original_name} → {new_name}\n"
        f"💾 {file_size / (1024**2):.2f} MB"
    )

    try:
        start = datetime.now()
        await message.download(file_name=file_path)
        duration = (datetime.now() - start).total_seconds()
        speed = (file_size / (1024**2)) / duration

        await status.edit_text(
            f"✅ Видео загружено!\n\n"
            f"📄 {new_name}\n"
            f"💾 {file_size / (1024**2):.2f} MB\n"
            f"⏱️ {duration:.1f}s | 🚀 {speed:.2f} MB/s"
        )
        logging.info(f"✅ Video saved: {file_path}")

        # Загрузка на платформы
        await upload_to_platforms(file_path, new_name, message, status, user_id)

    except Exception as e:
        await status.edit_text(f"❌ Ошибка: `{e}`")
        logging.error(f"Video download error: {e}")

async def upload_to_platforms(video_path, video_name, message, status_msg, user_id):
    """Загрузка на YouTube и/или ВК согласно выбору пользователя"""
    base_name = os.path.splitext(video_name)[0]
    json_path = os.path.join(CONFIG['download_folder'], f"{base_name}.json")

    # Читаем metadata
    if os.path.exists(json_path):
        with open(json_path, 'r', encoding='utf-8') as f:
            metadata = json.load(f)
        title = metadata.get('title', base_name)
        description = metadata.get('description', '')
        playlist_name = metadata.get('playlist')
        platforms = metadata.get('platforms', ['youtube'])  # по умолчанию youtube
        thumbnail_file = metadata.get('thumbnail', CONFIG['default_thumbnail'])
    else:
        title = base_name.replace('_', ' ')
        description = f"Загружено через Telegram: {title}"
        playlist_name = None
        platforms = ['youtube']
        thumbnail_file = None

    # Путь к thumbnail
    thumbnail_path = None
    if thumbnail_file:
        tp = os.path.join(CONFIG['download_folder'], thumbnail_file)
        if os.path.exists(tp):
            thumbnail_path = tp

    results = []

    # YOUTUBE
    if 'youtube' in platforms and youtube_uploader and CONFIG['youtube']['auto_upload']:
        await status_msg.edit_text(
            f"🎬 Загружаю на YouTube...\n📌 {title}"
        )
        yt_playlist_id = None
        if playlist_name and playlist_name in CONFIG['playlists']:
            yt_playlist_id = CONFIG['playlists'][playlist_name]

        yt_result = youtube_uploader.upload_video(
            video_path, title, description,
            thumbnail_path=thumbnail_path,
            playlist_id=yt_playlist_id
        )
        if yt_result['success']:
            results.append(f"📺 YouTube: {yt_result['url']}")
        else:
            results.append(f"📺 YouTube: ❌ {yt_result.get('error', '')[:80]}")

    # VK
    if 'vk' in platforms and vk_uploader:
        await status_msg.edit_text(
            f"📱 Загружаю в ВКонтакте...\n📌 {title}"
        )
        vk_owner_id = None
        if playlist_name and playlist_name in CONFIG.get('vk', {}).get('channels', {}):
            vk_owner_id = CONFIG['vk']['channels'][playlist_name]

        vk_result = vk_uploader.upload_video(
            video_path, title, description,
            owner_id=vk_owner_id,
            thumbnail_path=thumbnail_path
        )
        if vk_result['success']:
            results.append(f"📱 ВКонтакте: {vk_result['url']}")
        else:
            results.append(f"📱 ВКонтакте: ❌ {vk_result.get('error', '')[:80]}")

    # Финальное сообщение
    result_text = "\n".join(results) if results else "⚠️ Ни одна платформа не была выбрана"
    await status_msg.edit_text(
        f"✅✅ **Готово!**\n\n"
        f"📌 {title}\n\n"
        f"{result_text}\n\n"
        f"📋 Категория: {playlist_name or 'Нет'}\n"
        f"📸 Thumbnail: {'✅' if thumbnail_path else '❌'}"
    )

    # Перемещаем файлы в processed
    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    try:
        os.rename(video_path, os.path.join(CONFIG['processed_folder'], video_name))
    except Exception as e:
        logging.error(f"Ошибка перемещения видео: {e}")
    if os.path.exists(json_path):
        try:
            os.rename(json_path, os.path.join(CONFIG['processed_folder'], f"{base_name}.json"))
        except Exception as e:
            logging.error(f"Ошибка перемещения json: {e}")

    cleanup_stale_json(base_name)

# =============================================================================
# ДИАЛОГ: ТЕКСТ
# =============================================================================

@app.on_message(owner_filter & filters.text & ~filters.command(["start", "help", "stats"]))
async def handle_text(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return

    if user_id not in user_states:
        await message.reply_text(
            "📤 Отправьте файл:\n"
            "• Видео (.mp4, .mov, .avi, .mkv, .webm)\n"
            "• Документ (.pdf, .txt, .md)\n\n"
            "/help для справки"
        )
        return

    state = user_states[user_id]
    text = message.text.strip()

    # ШАГ 1: ССЫЛКА
    if state['waiting_for'] == 'link':
        state['link'] = text if text != '-' else ""
        state['waiting_for'] = 'playlist'

        playlists = list(CONFIG['playlists'].keys())
        playlist_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(playlists)])
        await message.reply_text(
            f"✅ Ссылка сохранена\n\n"
            f"📋 **Шаг 2/4:** Выберите категорию\n\n"
            f"{playlist_text}\n"
            f"0. Без категории\n\n"
            f"Отправьте номер:"
        )

    # ШАГ 2: ПЛЕЙЛИСТ / КАТЕГОРИЯ
    elif state['waiting_for'] == 'playlist':
        try:
            choice = int(text)
            playlists = list(CONFIG['playlists'].keys())

            if choice == 0:
                state['playlist'] = None
                name = "Без категории"
            elif 1 <= choice <= len(playlists):
                state['playlist'] = playlists[choice - 1]
                name = state['playlist']
            else:
                await message.reply_text(f"❌ Число от 0 до {len(playlists)}")
                return

            state['waiting_for'] = 'platforms'

            # Формируем список доступных платформ
            platform_lines = []
            platform_lines.append("1. 📺 YouTube")
            platform_lines.append("2. 📱 ВКонтакте")
            platform_lines.append("3. 📺📱 Обе платформы")
            platforms_text = "\n".join(platform_lines)

            await message.reply_text(
                f"✅ Категория: **{name}**\n\n"
                f"🌐 **Шаг 3/4:** Куда публиковать?\n\n"
                f"{platforms_text}\n\n"
                f"Отправьте номер:"
            )

        except ValueError:
            await message.reply_text("❌ Отправьте число")

    # ШАГ 3: ПЛАТФОРМЫ
    elif state['waiting_for'] == 'platforms':
        try:
            choice = int(text)
            if choice == 1:
                state['platforms'] = ['youtube']
                platform_name = "📺 YouTube"
            elif choice == 2:
                state['platforms'] = ['vk']
                platform_name = "📱 ВКонтакте"
            elif choice == 3:
                state['platforms'] = ['youtube', 'vk']
                platform_name = "📺📱 Обе платформы"
            else:
                await message.reply_text("❌ Число от 1 до 3")
                return

            state['waiting_for'] = 'thumbnail'

            await message.reply_text(
                f"✅ Платформа: **{platform_name}**\n\n"
                f"📸 **Шаг 4/4:** Отправьте thumbnail\n"
                f"(или `-` чтобы использовать unnamed.png)"
            )

        except ValueError:
            await message.reply_text("❌ Отправьте число")

    # ШАГ 4: ПРОПУСК THUMBNAIL
    elif state['waiting_for'] == 'thumbnail' and text == '-':
        await finalize_metadata(user_id, state, message, use_default_thumbnail=True)

    else:
        await message.reply_text("❓ Неожиданный ввод")

# =============================================================================
# ДИАЛОГ: ФОТО (THUMBNAIL)
# =============================================================================

@app.on_message(owner_filter & filters.photo)
async def handle_photo(client, message: Message):
    user_id = extract_user_id(message)
    if not user_id or not is_allowed_user(user_id):
        return

    if user_id not in user_states or user_states[user_id]['waiting_for'] != 'thumbnail':
        await message.reply_text("❓ Не ожидал изображение. Сначала отправьте документ.")
        return

    state = user_states[user_id]
    status = await message.reply_text("📸 Сохраняю thumbnail...")

    try:
        thumbnail_path = os.path.join(CONFIG['download_folder'], 'unnamed.png')
        await message.download(file_name=thumbnail_path)
        await status.edit_text("✅ Thumbnail сохранён!")
        await finalize_metadata(user_id, state, message, use_default_thumbnail=False)
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: {e}")
        logging.error(f"Thumbnail error: {e}")

# =============================================================================
# ФИНАЛИЗАЦИЯ METADATA
# =============================================================================

async def finalize_metadata(user_id, state, message, use_default_thumbnail=True):
    description = state['description']
    marker = "\n\nПоддержка: https://boosty.to/krastykovyaz"
    if state.get('link'):
        marker += f"\n\npaper - {state['link']}"
    marker += "\nПодписывайся - https://t.me/arxivpaper\nсоздано с помощью NotebookLM"
    description += marker

    metadata = {
        "title": state['title'],
        "description": description,
        "thumbnail": CONFIG['default_thumbnail'],
        "platforms": state.get('platforms', ['youtube'])
    }
    if state.get('playlist'):
        metadata["playlist"] = state['playlist']

    # Безопасное имя файла
    safe_title = "".join(
        c for c in state['title'] if c.isalnum() or c in (' ', '-', '_')
    ).strip().replace(' ', '_')[:50]

    original_ext = os.path.splitext(state['file_name'])[1]
    json_path = os.path.join(CONFIG['download_folder'], f"{safe_title}.json")
    doc_dest = os.path.join(CONFIG['processed_folder'], f"{safe_title}{original_ext}")

    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    os.rename(state['file_path'], doc_dest)

    platforms_str = " + ".join(metadata['platforms'])

    await message.reply_text(
        f"✅ **Metadata создан!**\n\n"
        f"📄 `{safe_title}.json`\n"
        f"📂 `{CONFIG['download_folder']}`\n\n"
        f"🌐 Платформы: **{platforms_str}**\n"
        f"📋 Категория: **{metadata.get('playlist') or 'Нет'}**\n"
        f"📸 Thumbnail: `{'unnamed.png' if use_default_thumbnail else 'сохранён'}`\n\n"
        f"💡 Отправьте видео → автозагрузится на **{platforms_str}**"
    )

    await message.reply_text(
        f"```json\n{json.dumps(metadata, indent=2, ensure_ascii=False)}\n```"
    )

    logging.info(f"✅ Metadata: {json_path}")
    pending_video_metadata_by_user[user_id] = safe_title
    del user_states[user_id]

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("  Telegram Bot + Gemini AI + YouTube + ВКонтакте")
    print("=" * 70)
    print(f"📂 Папка: {CONFIG['download_folder']}")
    print(f"👥 Пользователи: {CONFIG['allowed_users']}")
    print(f"🤖 Gemini: {CONFIG['gemini']['model']}")
    print(f"📺 YouTube: {'✅' if youtube_uploader else '❌'}")
    print(f"📱 ВКонтакте: {'✅' if vk_uploader else '❌'}")
    print()
    print("🚀 Запуск...")

    try:
        app.run()
    except KeyboardInterrupt:
        print("\n⏹️  Остановка...")

if __name__ == '__main__':
    main()