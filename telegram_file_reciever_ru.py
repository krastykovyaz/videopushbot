"""
Telegram бот для приёма файлов (до 2GB) с автоматической генерацией metadata
Использует pyrogram для файлов + Gemini AI для описаний из PDF/TXT
"""

import os
import json
import logging
from datetime import datetime
from pyrogram import Client, filters
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import asyncio
import PyPDF2
import google.generativeai as genai
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pickle
from glob import glob
from config_ru import CONFIG

# Настройка логирования
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

class GeminiGenerator:
    def __init__(self):
        genai.configure(api_key=CONFIG['gemini']['api_key'])
        self.model = genai.GenerativeModel(CONFIG['gemini']['model'])
        logging.info("✅ Gemini API инициализирован")
    
    def generate_description(self, title, content):
        """Генерация описания через Gemini"""
        if len(content) > 5000:
            content = content[:5000] + "..."
        
        prompt = f"""
Вы — эксперт в создании описаний для видео на YouTube.

НАЗВАНИЕ ДОКУМЕНТА: {title}
СОДЕРЖАНИЕ ДОКУМЕНТА: {content}

Создайте короткое, привлекательное описание для YouTube:
- 3-5 предложений (максимум 300 символов)
- Начните с убедительного вопроса или факта
- Простой язык, 2-3 эмодзи
- Без академического языка
- Верните ТОЛЬКО текст описания

Пример: «Изменит ли ИИ то, как мы познаём то, что знаем? 🧠✨ В этой статье рассматривается, как мы можем создавать системы ИИ, которым мы действительно можем доверять. Откройте для себя ключевые принципы эпистемической надёжности! 🤖📚»

Описание:
"""
        
        try:
            response = self.model.generate_content(prompt)
            return response.text.strip()
        except Exception as e:
            logging.error(f"❌ Gemini error: {e}")
            return f"Overview: {title}"

gemini_gen = GeminiGenerator()

# =============================================================================
# YOUTUBE UPLOADER
# =============================================================================

class YouTubeUploader:
    SCOPES = [
        "https://www.googleapis.com/auth/youtube",
        "https://www.googleapis.com/auth/youtube.upload",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]
    
    def __init__(self, client_secrets_file):
        self.client_secrets_file = client_secrets_file
        self.credentials = None
        self.youtube = None
        self._authenticate()
    
    def _authenticate(self):
        """Аутентификация в YouTube API"""
        creds = None
        token_file = 'youtube_token_ru.pickle'
        
        if os.path.exists(token_file):
            with open(token_file, 'rb') as token:
                creds = pickle.load(token)
            logging.info("YouTube: Загружены credentials")
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                logging.info("YouTube: Обновление токена...")
                creds.refresh(Request())
            else:
                logging.info("YouTube: Требуется авторизация...")
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, 
                    self.SCOPES,
                    redirect_uri='http://localhost:8080/'
                )
                creds = flow.run_local_server(port=8080)
                logging.info("YouTube: Авторизация успешна")
            
            with open(token_file, 'wb') as token:
                pickle.dump(creds, token)
        
        self.credentials = creds
        self.youtube = build('youtube', 'v3', credentials=creds)
        logging.info("✅ YouTube API готов")
    
    def upload_video(self, video_path, title, description, tags=None, thumbnail_path=None, playlist_id=None):
        """Загрузка видео на YouTube"""
        try:
            logging.info(f"YouTube: Начинаем загрузку '{title}'")
            
            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags or [],
                    'categoryId': CONFIG['youtube']['category_id']
                },
                'status': {
                    'privacyStatus': CONFIG['youtube']['privacy_status'],
                    'selfDeclaredMadeForKids': False,
                }
            }
            
            media = MediaFileUpload(
                video_path,
                chunksize=-1,
                resumable=True,
                mimetype='video/*'
            )
            
            request = self.youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logging.info(f"YouTube: Загружено {progress}%")
            
            video_id = response['id']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            
            # Задержка перед thumbnail
            if thumbnail_path and os.path.exists(thumbnail_path):
                logging.info("⏳ Ждём 3 сек перед загрузкой thumbnail...")
                import time
                time.sleep(3)
                self._upload_thumbnail(video_id, thumbnail_path)
            
            # Добавление в плейлист
            if playlist_id:
                self._add_to_playlist(video_id, playlist_id)
            
            logging.info(f"✅ YouTube: Видео загружено - {video_url}")
            return {'success': True, 'video_id': video_id, 'url': video_url}
            
        except Exception as e:
            logging.error(f"❌ YouTube: Ошибка - {str(e)}")
            return {'success': False, 'error': str(e)}
    
    def _upload_thumbnail(self, video_id, thumbnail_path):
        """Загрузка миниатюры"""
        try:
            logging.info(f"📸 Загрузка thumbnail: {os.path.basename(thumbnail_path)}")
            self.youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path)
            ).execute()
            logging.info("✅ Thumbnail загружен")
        except Exception as e:
            logging.error(f"❌ Ошибка thumbnail: {e}")
    
    def _add_to_playlist(self, video_id, playlist_id):
        """Добавление в плейлист"""
        try:
            logging.info(f"📋 Добавление в плейлист: {playlist_id}")
            self.youtube.playlistItems().insert(
                part="snippet",
                body={
                    'snippet': {
                        'playlistId': playlist_id,
                        'resourceId': {
                            'kind': 'youtube#video',
                            'videoId': video_id
                        }
                    }
                }
            ).execute()
            logging.info("✅ Добавлено в плейлист")
        except Exception as e:
            logging.error(f"❌ Ошибка плейлиста: {e}")

# Инициализация YouTube uploader (если включено)
youtube_uploader = None
if CONFIG['youtube']['auto_upload'] and os.path.exists(CONFIG['youtube']['client_secrets_file']):
    try:
        youtube_uploader = YouTubeUploader(CONFIG['youtube']['client_secrets_file'])
    except Exception as e:
        logging.warning(f"⚠️  YouTube uploader не инициализирован: {e}")

# =============================================================================
# TELEGRAM CLIENT
# =============================================================================

app = Client(
    "session_machiavelli",
    api_id=CONFIG['api_id'],
    api_hash=CONFIG['api_hash'],
    phone_number=CONFIG['phone_number']
)

# Хранилище для состояния пользователей (ожидание ввода)
user_states = {}

# =============================================================================
# УТИЛИТЫ
# =============================================================================

def is_allowed_user(user_id: int) -> bool:
    if not CONFIG['allowed_users']:
        return True
    return user_id in CONFIG['allowed_users']

def is_video_file(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in CONFIG['video_extensions']

def is_document_file(filename: str) -> bool:
    ext = os.path.splitext(filename)[1].lower()
    return ext in CONFIG['document_extensions']

def read_pdf(file_path):
    """Чтение PDF"""
    try:
        with open(file_path, 'rb') as f:
            pdf = PyPDF2.PdfReader(f)
            title = pdf.metadata.title if pdf.metadata and pdf.metadata.title else ""
            
            text = ""
            for page in pdf.pages[:5]:  # Первые 5 страниц
                text += page.extract_text()
            
            if not title and text:
                title = ' '.join(text.split('\n')[:3]).strip()[:100]
            
            return title, text
    except Exception as e:
        logging.error(f"PDF read error: {e}")
        return "", ""

def read_text(file_path):
    """Чтение TXT/MD"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        lines = content.split('\n')
        title = next((line.strip().lstrip('#').strip() for line in lines if line.strip()), "")
        
        return title, content
    except Exception as e:
        logging.error(f"Text read error: {e}")
        return "", ""

def read_document(file_path):
    """Универсальное чтение документа"""
    ext = os.path.splitext(file_path)[1].lower()
    if ext == '.pdf':
        return read_pdf(file_path)
    elif ext in ['.txt', '.md']:
        return read_text(file_path)
    return "", ""

# =============================================================================
# КОМАНДЫ
# =============================================================================

@app.on_message(filters.command("start"))
async def start_command(client, message: Message):
    user_id = message.from_user.id
    
    if not is_allowed_user(user_id):
        await message.reply_text(f"❌ Доступ запрещён.\nВаш ID: `{user_id}`")
        return
    
    await message.reply_text(
        "👋 **Telegram File Bot + AI**\n\n"
        "📤 **Загрузка видео:**\n"
        "Отправьте видео → оно сохранится в videos_to_upload/\n\n"
        "📄 **Загрузка PDF/TXT:**\n"
        "Отправьте документ → бот создаст metadata.json через Gemini AI\n\n"
        "Команды:\n"
        "/help - Справка\n"
        "/stats - Статистика"
    )

@app.on_message(filters.command("help"))
async def help_command(client, message: Message):
    await message.reply_text(
        "📖 **Инструкция:**\n\n"
        "**Видео (.mp4, .mov, .avi, .mkv, .webm):**\n"
        "1. Отправьте видео\n"
        "2. Файл сохранится в videos_to_upload/\n\n"
        "**Документы (.pdf, .txt, .md):**\n"
        "1. Отправьте PDF/TXT\n"
        "2. Бот прочитает документ\n"
        "3. Gemini сгенерирует описание\n"
        "4. Вы выберете плейлист\n"
        "5. Вы укажете ссылку на источник\n"
        "6. Создастся metadata.json\n\n"
        f"**Ваш ID:** `{message.from_user.id}`"
    )

@app.on_message(filters.command("stats"))
async def stats_command(client, message: Message):
    folder = CONFIG['download_folder']
    
    if not os.path.exists(folder):
        await message.reply_text("📂 Папка пустая")
        return
    
    files = [f for f in os.listdir(folder) if os.path.isfile(os.path.join(folder, f))]
    total_size = sum(os.path.getsize(os.path.join(folder, f)) for f in files)
    
    await message.reply_text(
        f"📊 **Статистика:**\n\n"
        f"📁 Файлов: {len(files)}\n"
        f"💾 Размер: {total_size / (1024**2):.2f} MB\n"
        f"📂 Папка: `{folder}`"
    )

# =============================================================================
# ОБРАБОТЧИК ВИДЕО
# =============================================================================
@app.on_message(filters.video)
async def handle_video(client, message: Message):
    user_id = message.from_user.id
    
    if not is_allowed_user(user_id):
        await message.reply_text(f"❌ Доступ запрещён. ID: `{user_id}`")
        return
    
    video = message.video
    original_file_name = video.file_name or f"video_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
    file_size = video.file_size
    
    if file_size > CONFIG['max_file_size']:
        await message.reply_text(
            f"❌ Файл слишком большой!\n"
            f"Размер: {file_size / (1024**3):.2f} GB\n"
            f"Макс: {CONFIG['max_file_size'] / (1024**3):.2f} GB"
        )
        return
    
    os.makedirs(CONFIG['download_folder'], exist_ok=True)
    
    # Ищем JSON файл
    json_files = glob(f"{CONFIG['download_folder']}/*.json")
    
    if not json_files:
        await message.reply_text(
            f"❌ Не найден metadata.json\n\n"
            f"Сначала отправьте PDF/TXT документ для создания metadata!"
        )
        logging.error("Metadata.json не найден")
        return
    
    # Берём первый JSON (предполагаем что он один)
    json_path = json_files[0]
    json_basename = os.path.splitext(os.path.basename(json_path))[0]
    
    # Новое имя видео = имя JSON файла
    _, ext = os.path.splitext(original_file_name)
    new_file_name = f"{json_basename}{ext}"
    file_path = os.path.join(CONFIG['download_folder'], new_file_name)
    
    status = await message.reply_text(
        f"📥 Загрузка видео...\n"
        f"📄 Оригинал: {original_file_name}\n"
        f"📝 Переименую в: {new_file_name}\n"
        f"💾 {file_size / (1024**2):.2f} MB"
    )
    
    try:
        start = datetime.now()
        await message.download(file_name=file_path)
        duration = (datetime.now() - start).total_seconds()
        speed = (file_size / (1024**2)) / duration
        
        await status.edit_text(
            f"✅ Видео загружено!\n\n"
            f"📄 {new_file_name}\n"
            f"💾 {file_size / (1024**2):.2f} MB\n"
            f"⏱️ {duration:.1f}s\n"
            f"🚀 {speed:.2f} MB/s"
        )
        
        logging.info(f"✅ Video saved: {file_path}")
        logging.info(f"   Переименовано: {original_file_name} → {new_file_name}")
        
        # АВТОМАТИЧЕСКАЯ ЗАГРУЗКА НА YOUTUBE
        if youtube_uploader and CONFIG['youtube']['auto_upload']:
            logging.info(f"🎬 Запуск загрузки на YouTube для: {new_file_name}")
            await upload_to_youtube_from_telegram(file_path, new_file_name, message, status)
        else:
            logging.warning("⚠️  YouTube uploader отключен или не инициализирован")
        
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: `{e}`")
        logging.error(f"Video download error: {e}")

async def upload_to_youtube_from_telegram(video_path, video_name, message, status_msg):
    """Загрузка видео на YouTube с поиском metadata"""
    try:
        # Ищем metadata.json
        base_name = os.path.splitext(video_name)[0]
        json_path = os.path.join(CONFIG['download_folder'], f"{base_name}.json")
        
        # Читаем metadata если есть
        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            
            title = metadata.get('title', base_name)
            description = metadata.get('description', '')
            playlist_name = metadata.get('playlist')
            thumbnail = metadata.get('thumbnail', CONFIG['default_thumbnail'])
            
            await status_msg.edit_text(
                f"✅ Видео загружено!\n\n"
                f"📄 {video_name}\n"
                f"📋 Найден metadata.json\n\n"
                f"🎬 Начинаю загрузку на YouTube...\n"
                f"📌 {title}"
            )
        else:
            # Без metadata - используем имя файла
            title = base_name.replace('_', ' ').replace('-', ' ')
            description = f"Загружено через Telegram: {title}"
            playlist_name = None
            thumbnail = None
            
            await status_msg.edit_text(
                f"✅ Видео загружено!\n\n"
                f"📄 {video_name}\n"
                f"⚠️  Metadata.json не найден\n\n"
                f"🎬 Загружаю на YouTube с названием:\n{title}"
            )
        
        # Получаем playlist_id
        playlist_id = None
        if playlist_name and playlist_name in CONFIG['playlists']:
            playlist_id = CONFIG['playlists'][playlist_name]
        
        # Путь к thumbnail
        thumbnail_path = None
        if thumbnail:
            thumb_path = os.path.join(CONFIG['download_folder'], thumbnail)
            if os.path.exists(thumb_path):
                thumbnail_path = thumb_path
        
        # Загружаем на YouTube
        result = youtube_uploader.upload_video(
            video_path,
            title,
            description,
            tags=None,
            thumbnail_path=thumbnail_path,
            playlist_id=playlist_id
        )
        
        if result['success']:
            video_url = result['url']
            
            await status_msg.edit_text(
                f"✅✅ **Загружено на YouTube!**\n\n"
                f"📄 {video_name}\n"
                f"📌 {title}\n\n"
                f"🔗 {video_url}\n\n"
                f"📋 Плейлист: {playlist_name or 'Нет'}\n"
                f"📸 Thumbnail: {'✅' if thumbnail_path else '❌'}"
            )
            
            # Перемещаем в processed
            os.makedirs(CONFIG['processed_folder'], exist_ok=True)
            processed_path = os.path.join(CONFIG['processed_folder'], video_name)
            os.rename(video_path, processed_path)
            
            # Также перемещаем metadata если был
            if os.path.exists(json_path):
                processed_json = os.path.join(CONFIG['processed_folder'], f"{base_name}.json")
                os.rename(json_path, processed_json)
            
            logging.info(f"✅ YouTube: {video_url}")
        else:
            await status_msg.edit_text(
                f"✅ Видео загружено локально\n"
                f"❌ Ошибка загрузки на YouTube:\n`{result.get('error')}`"
            )
    
    except Exception as e:
        logging.error(f"YouTube upload error: {e}")
        await status_msg.edit_text(
            f"✅ Видео загружено локально\n"
            f"❌ Ошибка YouTube: `{e}`"
        )

# =============================================================================
# ОБРАБОТЧИК ДОКУМЕНТОВ (PDF/TXT) - С ГЕНЕРАЦИЕЙ METADATA
# =============================================================================

@app.on_message(filters.document)
async def handle_document(client, message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    
    if not is_allowed_user(user_id):
        await message.reply_text(f"❌ Доступ запрещён. ID: `{user_id}`")
        return
    
    doc = message.document
    file_name = doc.file_name or f"doc_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    file_size = doc.file_size
    
    # Проверяем тип файла
    if not is_document_file(file_name) and not is_video_file(file_name):
        await message.reply_text(
            f"❌ Неподдерживаемый формат!\n\n"
            f"Видео: {', '.join(CONFIG['video_extensions'])}\n"
            f"Документы: {', '.join(CONFIG['document_extensions'])}"
        )
        return
    
    if file_size > CONFIG['max_file_size']:
        await message.reply_text(
            f"❌ Файл > {CONFIG['max_file_size'] / (1024**3):.1f} GB"
        )
        return
    
    os.makedirs(CONFIG['download_folder'], exist_ok=True)
    file_path = os.path.join(CONFIG['download_folder'], file_name)
    
    if os.path.exists(file_path):
        base, ext = os.path.splitext(file_name)
        file_name = f"{base}_{datetime.now().strftime('%Y%m%d_%H%M%S')}{ext}"
        file_path = os.path.join(CONFIG['download_folder'], file_name)
    
    status = await message.reply_text(
        f"📥 Загрузка...\n📄 {file_name}\n💾 {file_size / (1024**2):.2f} MB"
    )
    
    try:
        await message.download(file_name=file_path)
        
        # ЕСЛИ ЭТО ДОКУМЕНТ - ЗАПУСКАЕМ ДИАЛОГ
        if is_document_file(file_name):
            await status.edit_text("📄 Файл загружен! Читаю документ...")
            
            # Читаем документ
            title, content = read_document(file_path)
            
            if not title or not content:
                await status.edit_text("❌ Не удалось прочитать документ")
                return
            
            title = title.replace('\n', ' ').strip()[:100]
            
            await status.edit_text(
                f"📖 Документ прочитан!\n\n"
                f"📌 Заголовок:\n{title}\n\n"
                f"🤖 Генерирую описание через Gemini..."
            )
            
            # Генерируем описание
            description = gemini_gen.generate_description(title, content)
            
            # Сохраняем состояние и НАЧИНАЕМ ДИАЛОГ
            user_states[user_id] = {
                'file_name': file_name,
                'file_path': file_path,
                'title': title,
                'description': description,
                'waiting_for': 'link',  # Начинаем со ссылки
                'step': 1
            }
            
            await status.edit_text(
                f"✅ Описание сгенерировано!\n\n"
                f"📌 **Заголовок:**\n{title}\n\n"
                f"📝 **Описание:**\n{description[:200]}...\n\n"
                f"🔗 **Шаг 1/3:** Отправьте ссылку на источник\n"
                f"(или `-` если нет ссылки)"
            )
        
        else:
            # Обычное видео
            await status.edit_text(
                f"✅ Файл загружен!\n\n📄 {file_name}"
            )
            logging.info(f"✅ File saved: {file_path}")
        
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: `{e}`")
        logging.error(f"Document error: {e}")

# =============================================================================
# ОБРАБОТЧИК ТЕКСТА И ИЗОБРАЖЕНИЙ - ПОСЛЕДОВАТЕЛЬНЫЙ ДИАЛОГ
# =============================================================================

@app.on_message(filters.text & ~filters.command(["start", "help", "stats"]))
async def handle_text(client, message: Message):
    user_id = message.from_user.id
    
    if not is_allowed_user(user_id):
        return
    
    # Проверяем ожидаем ли мы ввод от пользователя
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
        state['step'] = 2
        
        # Показываем список плейлистов
        playlists = list(CONFIG['playlists'].keys())
        playlist_text = "\n".join([f"{i+1}. {name}" for i, name in enumerate(playlists)])
        
        await message.reply_text(
            f"✅ Ссылка сохранена\n\n"
            f"📋 **Шаг 2/3:** Выберите плейлист\n\n"
            f"{playlist_text}\n"
            f"0. Без плейлиста\n\n"
            f"Отправьте номер (0-{len(playlists)}):"
        )
    
    # ШАГ 2: ПЛЕЙЛИСТ
    elif state['waiting_for'] == 'playlist':
        try:
            choice = int(text)
            playlists = list(CONFIG['playlists'].keys())
            
            if choice == 0:
                state['playlist'] = None
                playlist_name = "Без плейлиста"
            elif 1 <= choice <= len(playlists):
                state['playlist'] = playlists[choice - 1]
                playlist_name = state['playlist']
            else:
                await message.reply_text(
                    f"❌ Неверный номер. Отправьте число от 0 до {len(playlists)}"
                )
                return
            
            state['waiting_for'] = 'thumbnail'
            state['step'] = 3
            
            await message.reply_text(
                f"✅ Плейлист: **{playlist_name}**\n\n"
                f"📸 **Шаг 3/3:** Отправьте изображение для thumbnail\n"
                f"(или отправьте `-` чтобы использовать unnamed.png)"
            )
            
        except ValueError:
            await message.reply_text("❌ Отправьте число (номер плейлиста)")
    
    # ШАГ 3: ПРОПУСК THUMBNAIL (если отправили "-")
    elif state['waiting_for'] == 'thumbnail' and text == '-':
        # Используем дефолтный thumbnail
        await finalize_metadata(user_id, state, message, use_default_thumbnail=True)
    
    else:
        await message.reply_text("❓ Неожиданный ввод")

# =============================================================================
# ОБРАБОТЧИК ИЗОБРАЖЕНИЙ (ШАГ 3: THUMBNAIL)
# =============================================================================

@app.on_message(filters.photo)
async def handle_photo(client, message: Message):
    user_id = message.from_user.id
    
    if not is_allowed_user(user_id):
        return
    
    # Проверяем ожидаем ли thumbnail
    if user_id not in user_states or user_states[user_id]['waiting_for'] != 'thumbnail':
        await message.reply_text("❓ Я не ожидал изображение. Отправьте документ сначала.")
        return
    
    state = user_states[user_id]
    
    status = await message.reply_text("📸 Сохраняю thumbnail...")
    
    try:
        # Скачиваем изображение как unnamed.png
        thumbnail_path = os.path.join(CONFIG['download_folder'], 'unnamed.png')
        await message.download(file_name=thumbnail_path)
        
        await status.edit_text("✅ Thumbnail сохранён!")
        
        # Завершаем создание metadata
        await finalize_metadata(user_id, state, message, use_default_thumbnail=False)
        
    except Exception as e:
        await status.edit_text(f"❌ Ошибка сохранения thumbnail: {e}")
        logging.error(f"Thumbnail save error: {e}")

# =============================================================================
# ФИНАЛИЗАЦИЯ METADATA
# =============================================================================

async def finalize_metadata(user_id, state, message, use_default_thumbnail=True):
    """Создание финального metadata.json и переименование файлов"""
    
    # Формируем финальное описание
    description = state['description']
    marker = "\n\nПоддержка: https://boosty.to/krastykovyaz"
    if state.get('link'):
        marker += f"\n\npaper - {state['link']}"
    marker += "\nПодписывайся - https://t.me/arxivpaper\nсоздано с помощью NotebookLM"
    description += marker
    
    # Создаём metadata
    metadata = {
        "title": state['title'],
        "description": description,
        "thumbnail": CONFIG['default_thumbnail']
    }
    
    if state.get('playlist'):
        metadata["playlist"] = state['playlist']
    
    # Генерируем безопасное имя файла из заголовка
    safe_title = "".join(c for c in state['title'] if c.isalnum() or c in (' ', '-', '_')).strip()
    safe_title = safe_title.replace(' ', '_')[:50]  # Макс 50 символов
    
    # Определяем расширение оригинального документа
    original_ext = os.path.splitext(state['file_name'])[1]
    
    # Новые имена файлов
    new_doc_name = f"{safe_title}{original_ext}"
    new_json_name = f"{safe_title}.json"
    
    # Пути
    json_path = os.path.join(CONFIG['download_folder'], new_json_name)
    new_doc_path = os.path.join(CONFIG['processed_folder'], new_doc_name)
    
    # Сохраняем JSON
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)
    
    # Перемещаем документ в processed с новым именем
    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    os.rename(state['file_path'], new_doc_path)
    
    await message.reply_text(
        f"✅ **Всё готово!**\n\n"
        f"📄 **Metadata:** `{new_json_name}`\n"
        f"📂 Папка: `{CONFIG['download_folder']}`\n\n"
        f"📦 **Документ:** `{new_doc_name}`\n"
        f"📂 Перемещён в: `{CONFIG['processed_folder']}`\n\n"
        f"📸 **Thumbnail:** `{'unnamed.png' if use_default_thumbnail else 'сохранён'}`\n\n"
        f"💡 **Теперь отправьте видео с именем:**\n"
        f"`{safe_title}.mp4`\n\n"
        f"И YouTube uploader автоматически подхватит metadata!"
    )
    
    # Показываем JSON
    await message.reply_text(
        f"```json\n{json.dumps(metadata, indent=2, ensure_ascii=False)}\n```"
    )
    
    logging.info(f"✅ Metadata created: {json_path}")
    logging.info(f"✅ Document renamed to: {new_doc_path}")
    
    # Очищаем состояние
    del user_states[user_id]

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("  Telegram Bot + Gemini AI")
    print("  Загрузка файлов + генерация metadata")
    print("=" * 70)
    print()
    print(f"📂 Папка: {CONFIG['download_folder']}")
    print(f"👥 Пользователи: {CONFIG['allowed_users']}")
    print(f"🤖 Gemini: {CONFIG['gemini']['model']}")
    print()
    print("🚀 Запуск...")
    print()
    
    try:
        app.run()
    except KeyboardInterrupt:
        print("\n⏹️  Остановка...")
        print("✅ Завершено")

if __name__ == '__main__':
    main()