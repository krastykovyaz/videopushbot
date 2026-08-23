"""
Простой загрузчик видео на YouTube
Мониторит папку и автоматически загружает новые видео
"""

import os
import time
import json
import logging
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import pickle
from config import CONFIG

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('youtube_upload.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# =============================================================================
# КОНФИГУРАЦИЯ - НАСТРОЙТЕ ЭТИ ПАРАМЕТРЫ
# =============================================================================

CONFIG = {
    # Папка для мониторинга
    'watch_folder': './videos_to_upload',
    
    # Папка для обработанных видео
    'processed_folder': './uploaded_videos',
    
    # YouTube настройки
    'youtube': {
        'client_secrets_file': 'client_secret_youtuberu.json',
        'privacy_status': 'private',  # 'public', 'private' или 'unlisted'
        'category_id': '22',  # 22 = People & Blogs
        
        # Плейлисты (название → ID плейлиста)
        # Получите ID: откройте плейлист на YouTube, ID в URL после list=
        'playlists': {
        'AI Paper Review': 'PLvt0scCYQch9n6YlEEIfwpf1L179ARuIz',
        'Crypto Ideas': 'PLvt0scCYQch-1I1Y_u1zG_nwZiHlFIeoB',
        'GPMorgan report debates': 'PLvt0scCYQch_bu0yaNc4BhtKVcLuOwpF2',
        'The Economist': 'PLvt0scCYQch8zx--csTddS3EyLoGSOMrT',
        'The National Geo talks': 'PLvt0scCYQch8vJrVT_fDDieiorqBw_41k',
        'Auto Detail': 'PLvt0scCYQch9KyiRaPyOP1C4T6Xd1qMUW'
    },
    },
    
    # Поддерживаемые форматы
    'video_extensions': ['.mp4', '.mov', '.avi', '.mkv', '.flv', '.webm'],
    'thumbnail_extensions': ['.jpg', '.jpeg', '.png'],
    
    # Количество попыток при ошибке
    'max_retries': 3,
    'retry_delay': 60,
}

# YouTube категории для справки:
# 1  - Film & Animation
# 2  - Autos & Vehicles
# 10 - Music
# 15 - Pets & Animals
# 17 - Sports
# 19 - Travel & Events
# 20 - Gaming
# 22 - People & Blogs
# 23 - Comedy
# 24 - Entertainment
# 25 - News & Politics
# 26 - Howto & Style
# 27 - Education
# 28 - Science & Technology

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
        token_file = 'youtube_token.pickle'
        
        # Загружаем сохранённые credentials
        if os.path.exists(token_file):
            with open(token_file, 'rb') as token:
                creds = pickle.load(token)
            logging.info("YouTube: Загружены сохранённые credentials")
        
        # Если нет валидных credentials - получаем новые
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
            
            # Сохраняем credentials
            with open(token_file, 'wb') as token:
                pickle.dump(creds, token)
            logging.info("YouTube: Credentials сохранены")
        
        self.credentials = creds
        self.youtube = build('youtube', 'v3', credentials=creds)
        logging.info("✅ YouTube: Готов к загрузке")
    
    def upload_video(self, video_path, title, description, tags=None, thumbnail_path=None, playlist_id=None):
        """Загрузка видео на YouTube"""
        try:
            logging.info(f"YouTube: Начинаем загрузку '{title}'")
            
            # Метаданные видео
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
            
            # Подготовка файла
            media = MediaFileUpload(
                video_path,
                chunksize=-1,
                resumable=True,
                mimetype='video/*'
            )
            
            # Создание запроса
            request = self.youtube.videos().insert(
                part=','.join(body.keys()),
                body=body,
                media_body=media
            )
            
            # Загрузка с прогрессом
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status:
                    progress = int(status.progress() * 100)
                    logging.info(f"YouTube: Загружено {progress}%")
            
            video_id = response['id']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            
            # Задержка перед загрузкой thumbnail (YouTube нужно время на обработку видео)
            if thumbnail_path and os.path.exists(thumbnail_path):
                logging.info("⏳ Ждём 3 секунды перед загрузкой thumbnail...")
                time.sleep(3)
                self._upload_thumbnail(video_id, thumbnail_path)
            
            # Добавление в плейлист (если указан)
            if playlist_id:
                self._add_to_playlist(video_id, playlist_id)
            
            logging.info(f"✅ YouTube: Видео загружено - {video_url}")
            return {
                'success': True, 
                'video_id': video_id, 
                'url': video_url
            }
            
        except Exception as e:
            logging.error(f"❌ YouTube: Ошибка - {str(e)}")
            return {
                'success': False, 
                'error': str(e)
            }
    
    def _upload_thumbnail(self, video_id, thumbnail_path):
        """Загрузка миниатюры для видео"""
        try:
            logging.info(f"📸 Загрузка thumbnail: {os.path.basename(thumbnail_path)}")
            
            self.youtube.thumbnails().set(
                videoId=video_id,
                media_body=MediaFileUpload(thumbnail_path)
            ).execute()
            
            logging.info("✅ Thumbnail загружен")
        except Exception as e:
            logging.error(f"❌ Ошибка загрузки thumbnail: {e}")
    
    def _add_to_playlist(self, video_id, playlist_id):
        """Добавление видео в плейлист"""
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
            logging.error(f"❌ Ошибка добавления в плейлист: {e}")

# =============================================================================
# FILE WATCHER
# =============================================================================

class VideoUploadHandler(FileSystemEventHandler):
    def __init__(self, youtube_uploader):
        self.youtube_uploader = youtube_uploader
        self.processing = set()
    
    def on_created(self, event):
        """Вызывается когда новый файл появляется"""
        if event.is_directory:
            return
        
        file_path = event.src_path
        file_ext = os.path.splitext(file_path)[1].lower()
        
        if file_ext not in CONFIG['video_extensions']:
            return
        
        if file_path in self.processing:
            return
        
        # Ждём завершения копирования
        time.sleep(5)
        
        logging.info(f"📁 Обнаружен: {os.path.basename(file_path)}")
        self.processing.add(file_path)
        self.process_video(file_path)
        self.processing.remove(file_path)
    
    def process_video(self, video_path):
        """Обработка и загрузка видео"""
        video_name = os.path.basename(video_path)
        base_name = os.path.splitext(video_name)[0]
        video_dir = os.path.dirname(video_path)
        
        # =====================================================================
        # ЧТЕНИЕ МЕТАДАННЫХ
        # =====================================================================
        
        # Ищем metadata.json (приоритет)
        metadata_file = os.path.join(video_dir, f"{base_name}.json")
        metadata = {}
        
        if os.path.exists(metadata_file):
            with open(metadata_file, 'r', encoding='utf-8') as f:
                metadata = json.load(f)
            logging.info(f"📄 Найден metadata.json")
        
        # Или ищем файл с описанием .txt
        description_file = os.path.join(video_dir, f"{base_name}.txt")
        
        if os.path.exists(description_file) and not metadata:
            with open(description_file, 'r', encoding='utf-8') as f:
                content = f.read()
            
            lines = content.split('\n')
            description_lines = []
            tags = []
            
            for line in lines:
                if line.strip().startswith('#tags:'):
                    tags_str = line.replace('#tags:', '').strip()
                    tags = [t.strip() for t in tags_str.split(',')]
                else:
                    description_lines.append(line)
            
            metadata = {
                'description': '\n'.join(description_lines).strip(),
                'tags': tags
            }
            logging.info(f"📝 Найдено описание в .txt")
        
        # =====================================================================
        # ИЗВЛЕЧЕНИЕ ДАННЫХ
        # =====================================================================
        
        # Название (приоритет: metadata → имя файла)
        title = metadata.get('title', base_name.replace('_', ' ').replace('-', ' '))
        
        # Описание
        description = metadata.get('description', f"Загружено автоматически: {title}")
        
        # Теги
        tags = metadata.get('tags', [])
        
        # Thumbnail (приоритет: metadata → base_name.jpg/png)
        thumbnail_path = metadata.get('thumbnail')
        
        # Если указан относительный путь — ищем в папке с видео
        if thumbnail_path and not os.path.isabs(thumbnail_path):
            thumbnail_path = os.path.join(video_dir, thumbnail_path)
        
        # Если не указан — ищем с тем же именем
        if not thumbnail_path or not os.path.exists(thumbnail_path):
            for ext in CONFIG['thumbnail_extensions']:
                potential_thumb = os.path.join(video_dir, f"{base_name}{ext}")
                if os.path.exists(potential_thumb):
                    thumbnail_path = potential_thumb
                    break
        
        # Проверяем что файл существует
        if thumbnail_path and not os.path.exists(thumbnail_path):
            logging.warning(f"⚠️  Thumbnail не найден: {thumbnail_path}")
            thumbnail_path = None
        
        # Проверяем размер файла (YouTube требует < 2MB)
        if thumbnail_path:
            thumb_size = os.path.getsize(thumbnail_path) / (1024 * 1024)  # в MB
            if thumb_size > 2:
                logging.warning(f"⚠️  Thumbnail слишком большой: {thumb_size:.2f}MB (максимум 2MB)")
                logging.warning(f"⚠️  Пропускаем загрузку thumbnail")
                thumbnail_path = None
        
        # Плейлист
        playlist_name = metadata.get('playlist')
        playlist_id = None
        
        if playlist_name:
            playlist_id = CONFIG['youtube']['playlists'].get(playlist_name)
            if playlist_id:
                logging.info(f"📋 Плейлист: {playlist_name}")
            else:
                logging.warning(f"⚠️  Плейлист '{playlist_name}' не найден в CONFIG")
        
        # =====================================================================
        # ВЫВОД ИНФОРМАЦИИ
        # =====================================================================
        
        logging.info(f"📹 Название: {title}")
        logging.info(f"📝 Описание: {len(description)} символов")
        if tags:
            logging.info(f"🏷️  Теги: {', '.join(tags)}")
        if thumbnail_path:
            logging.info(f"📸 Thumbnail: {os.path.basename(thumbnail_path)}")
        
        # =====================================================================
        # ЗАГРУЗКА
        # =====================================================================
        
        logging.info(f"🚀 Загрузка на YouTube...")
        
        result = self._upload_with_retry(
            lambda: self.youtube_uploader.upload_video(
                video_path, title, description, tags, thumbnail_path, playlist_id
            )
        )
        
        # Перемещаем файлы
        if result['success']:
            self._move_to_processed(video_path)
            if os.path.exists(description_file):
                self._move_to_processed(description_file)
            if os.path.exists(metadata_file):
                self._move_to_processed(metadata_file)
            if thumbnail_path and os.path.exists(thumbnail_path):
                self._move_to_processed(thumbnail_path)
            self._save_result(video_name, result, metadata)
        else:
            logging.error(f"❌ Не удалось загрузить {video_name}")
    
    def _upload_with_retry(self, upload_func):
        """Загрузка с повторными попытками"""
        for attempt in range(1, CONFIG['max_retries'] + 1):
            logging.info(f"🔄 Попытка {attempt}/{CONFIG['max_retries']}")
            result = upload_func()
            
            if result['success']:
                return result
            
            if attempt < CONFIG['max_retries']:
                logging.warning(f"⏳ Повтор через {CONFIG['retry_delay']} сек...")
                time.sleep(CONFIG['retry_delay'])
        
        return result
    
    def _move_to_processed(self, file_path):
        """Перемещение в папку обработанных"""
        processed_path = os.path.join(
            CONFIG['processed_folder'],
            os.path.basename(file_path)
        )
        os.makedirs(CONFIG['processed_folder'], exist_ok=True)
        os.rename(file_path, processed_path)
        logging.info(f"📦 Перемещено: {processed_path}")
    
    def _save_result(self, video_name, result, metadata=None):
        """Сохранение результатов в JSON"""
        record = {
            'video': video_name,
            'timestamp': datetime.now().isoformat(),
            'url': result.get('url'),
            'video_id': result.get('video_id'),
            'title': metadata.get('title') if metadata else None,
            'playlist': metadata.get('playlist') if metadata else None,
        }
        
        results_file = 'youtube_uploads.json'
        
        if os.path.exists(results_file):
            with open(results_file, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
        else:
            all_results = []
        
        all_results.append(record)
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)
        
        logging.info(f"💾 Сохранено в {results_file}")

# =============================================================================
# MAIN
# =============================================================================

def main():
    print("=" * 70)
    print("  YouTube Auto Uploader")
    print("  Автоматическая загрузка видео на YouTube")
    print("=" * 70)
    print()
    
    # Проверка client_secret.json
    if not os.path.exists(CONFIG['youtube']['client_secrets_file']):
        print("❌ ОШИБКА: client_secret.json не найден!")
        print()
        print("Получите файл:")
        print("1. https://console.cloud.google.com/")
        print("2. APIs & Services → Credentials")
        print("3. CREATE CREDENTIALS → OAuth client ID")
        print("4. Application type: Web application")
        print("5. Authorized redirect URIs: http://localhost:8080/")
        print("6. Скачайте JSON → переименуйте в client_secret.json")
        print()
        return
    
    # Создание папок
    os.makedirs(CONFIG['watch_folder'], exist_ok=True)
    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    
    # Инициализация
    print("🔐 Инициализация YouTube API...")
    youtube_uploader = YouTubeUploader(CONFIG['youtube']['client_secrets_file'])
    
    # Мониторинг
    event_handler = VideoUploadHandler(youtube_uploader)
    observer = Observer()
    observer.schedule(event_handler, CONFIG['watch_folder'], recursive=False)
    observer.start()
    
    print()
    print(f"👀 Мониторинг: {CONFIG['watch_folder']}")
    print(f"📊 Приватность: {CONFIG['youtube']['privacy_status']}")
    print(f"📂 Обработанные → {CONFIG['processed_folder']}")
    print()
    print("=" * 70)
    print("📝 ФОРМАТЫ МЕТАДАННЫХ:")
    print("=" * 70)
    print()
    print("ВАРИАНТ 1: JSON файл (рекомендуется)")
    print("-" * 70)
    print("Создайте my_video.json рядом с my_video.mp4:")
    print()
    print('{')
    print('  "title": "Название видео",')
    print('  "description": "Описание видео\\n\\nМожет быть многострочным",')
    print('  "tags": ["тег1", "тег2", "тег3"],')
    print('  "thumbnail": "my_video.jpg",  // или абсолютный путь')
    print('  "playlist": "Podcast 1"  // название из CONFIG')
    print('}')
    print()
    print("ВАРИАНТ 2: TXT файл (простой)")
    print("-" * 70)
    print("Создайте my_video.txt:")
    print()
    print("Описание видео")
    print()
    print("#tags: тег1, тег2, тег3")
    print()
    print("+ my_video.jpg (thumbnail) - опционально")
    print()
    print("ВАРИАНТ 3: Только видео")
    print("-" * 70)
    print("Просто my_video.mp4 - название из имени файла")
    print()
    print("=" * 70)
    print("📋 ПЛЕЙЛИСТЫ:")
    print("=" * 70)
    playlists = CONFIG['youtube'].get('playlists', {})
    if playlists:
        for name, playlist_id in playlists.items():
            print(f"  • {name}: {playlist_id}")
    else:
        print("  Плейлисты не настроены")
    print()
    print("  Чтобы добавить видео в плейлист:")
    print("  1. Найдите ID плейлиста (URL: ...list=PLxxxxxxx)")
    print("  2. Добавьте в CONFIG['youtube']['playlists']")
    print("  3. Укажите название в metadata.json")
    print()
    print("=" * 70)
    print("⏹️  Для остановки: Ctrl+C")
    print("=" * 70)
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        print("\n\n⏹️  Остановка...")
    
    observer.join()
    print("✅ Завершено")

if __name__ == '__main__':
    main()