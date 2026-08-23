"""
Скрипт для автоматической загрузки видео на YouTube, Spotify, Boosty и Patreon
Мониторит папку и автоматически загружает новые файлы
"""

import os
import time
import json
import logging
from pathlib import Path
from datetime import datetime
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
import requests
import pickle

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('upload_log_full.txt', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# =============================================================================
# КОНФИГУРАЦИЯ - НАСТРОЙТЕ ЭТИ ПАРАМЕТРЫ
# =============================================================================

CONFIG = {
    # Папка для мониторинга (где появляются новые видео)
    'watch_folder': './videos_to_upload',
    
    # Папка для обработанных видео
    'processed_folder': './processed_videos',
    
    # YouTube настройки
    'youtube': {
        'client_secrets_file': 'client_secret.json',
        'privacy_status': 'private',  # 'public', 'private' или 'unlisted'
        'category_id': '22',
    },
    
    # Spotify Creators настройки (ваш Client ID уже вставлен)
    'spotify': {
        'client_id': 'e193779f228e4d16b5428ea170741f62',  # Ваш Client ID
        'client_secret': 'ВСТАВЬТЕ_CLIENT_SECRET_СЮДА',   # Dashboard → Settings → View client secret
        # Токены заполняются автоматически из spotify_tokens.json после запуска get_spotify_token.py
        'access_token': '',
        'refresh_token': '',
    },
    
    # Boosty настройки
    'boosty': {
        'access_token': 'YOUR_BOOSTY_ACCESS_TOKEN',
        'blog_name': 'your_blog_name',  # Имя вашего блога на boosty
        'price': 0,  # 0 для бесплатного доступа, или сумма в рублях
        'teaser': '',  # Превью для подписчиков (необязательно)
    },
    
    # Patreon настройки
    'patreon': {
        'access_token': 'YOUR_PATREON_ACCESS_TOKEN',
        'campaign_id': 'YOUR_CAMPAIGN_ID',
        'tier_ids': [],  # Список tier_id для ограничения доступа (пусто = все)
        'is_paid': False,  # True - только для платных, False - для всех
    },
    
    # Поддерживаемые форматы
    'video_extensions': ['.mp4', '.mov', '.avi', '.mkv', '.flv'],
    
    # Количество попыток при ошибке
    'max_retries': 3,
    'retry_delay': 60,
}

# =============================================================================
# YOUTUBE UPLOADER
# =============================================================================

class YouTubeUploader:
    SCOPES = ['https://www.googleapis.com/auth/youtube.upload']
    
    def __init__(self, client_secrets_file):
        self.client_secrets_file = client_secrets_file
        self.credentials = None
        self.youtube = None
        self._authenticate()
    
    def _authenticate(self):
        creds = None
        token_file = 'youtube_token.pickle'
        
        if os.path.exists(token_file):
            with open(token_file, 'rb') as token:
                creds = pickle.load(token)
        
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.client_secrets_file, self.SCOPES)
                creds = flow.run_local_server(port=0)
            
            with open(token_file, 'wb') as token:
                pickle.dump(creds, token)
        
        self.credentials = creds
        self.youtube = build('youtube', 'v3', credentials=creds)
        logging.info("YouTube: Аутентификация успешна")
    
    def upload_video(self, video_path, title, description, tags=None):
        try:
            body = {
                'snippet': {
                    'title': title,
                    'description': description,
                    'tags': tags or [],
                    'categoryId': CONFIG['youtube']['category_id']
                },
                'status': {
                    'privacyStatus': CONFIG['youtube']['privacy_status']
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
                    logging.info(f"YouTube: Загружено {int(status.progress() * 100)}%")
            
            video_id = response['id']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            logging.info(f"YouTube: Видео загружено успешно - {video_url}")
            return {'success': True, 'video_id': video_id, 'url': video_url}
            
        except Exception as e:
            logging.error(f"YouTube: Ошибка загрузки - {str(e)}")
            return {'success': False, 'error': str(e)}

# =============================================================================
# SPOTIFY CREATORS UPLOADER (creators.spotify.com)
# =============================================================================

class SpotifyUploader:
    """
    Загрузчик для Spotify Creators (creators.spotify.com / anchor.fm)
    
    ВАЖНО: Это НЕ Web API (api.spotify.com) — тот для плееров, НЕ для загрузки.
    Для загрузки эпизодов используется Creators API.
    
    Первый запуск: выполните get_spotify_token.py для получения токенов.
    """
    
    CREATORS_API = "https://api.anchor.fm/v3"
    
    def __init__(self, client_id, client_secret, access_token='', refresh_token=''):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.show_id = None
        self._load_tokens_from_file()
        if self.access_token:
            self.show_id = self._get_show_id()
            logging.info(f"Spotify Creators: Готов, Show ID: {self.show_id}")
        else:
            logging.warning("Spotify Creators: Нет токена! Запустите get_spotify_token.py")
    
    def _load_tokens_from_file(self):
        if os.path.exists("spotify_tokens.json"):
            with open("spotify_tokens.json", "r") as f:
                data = json.load(f)
            if not self.access_token:
                self.access_token = data.get("access_token", "")
            if not self.refresh_token:
                self.refresh_token = data.get("refresh_token", "")
            logging.info("Spotify Creators: Токены загружены из файла")
    
    def _save_tokens(self):
        with open("spotify_tokens.json", "w") as f:
            json.dump({"access_token": self.access_token, "refresh_token": self.refresh_token}, f, indent=2)
    
    def _refresh_access_token(self):
        import base64
        if not self.refresh_token:
            logging.error("Spotify Creators: Нет refresh token, запустите get_spotify_token.py")
            return False
        creds = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            headers={"Authorization": f"Basic {creds}", "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "refresh_token", "refresh_token": self.refresh_token}
        )
        if resp.status_code == 200:
            data = resp.json()
            self.access_token = data["access_token"]
            if "refresh_token" in data:
                self.refresh_token = data["refresh_token"]
            self._save_tokens()
            logging.info("Spotify Creators: Токен обновлен автоматически")
            return True
        logging.error(f"Spotify Creators: Ошибка обновления токена: {resp.text}")
        return False
    
    def _headers(self):
        return {"Authorization": f"Bearer {self.access_token}"}
    
    def _req(self, method, url, **kwargs):
        resp = getattr(requests, method)(url, headers=self._headers(), **kwargs)
        if resp.status_code == 401 and self._refresh_access_token():
            resp = getattr(requests, method)(url, headers=self._headers(), **kwargs)
        return resp
    
    def _get_show_id(self):
        try:
            resp = self._req("get", f"{self.CREATORS_API}/shows")
            if resp.status_code == 200:
                shows = resp.json()
                if shows:
                    return shows[0].get("showId") or shows[0].get("id")
        except Exception as e:
            logging.error(f"Spotify Creators: Ошибка получения Show ID - {e}")
        return None
    
    def upload_video(self, video_path, title, description):
        try:
            if not self.access_token:
                raise Exception("Нет токена. Запустите get_spotify_token.py")
            if not self.show_id:
                self.show_id = self._get_show_id()
                if not self.show_id:
                    raise Exception("Не найден Show ID. Проверьте авторизацию.")
            
            # Шаг 1: Запросить upload URL
            init = self._req("post", f"{self.CREATORS_API}/shows/{self.show_id}/episodes/upload",
                             json={"mimeType": "video/mp4"})
            if init.status_code not in [200, 201]:
                raise Exception(f"Ошибка инициализации: {init.text}")
            
            init_data = init.json()
            upload_url = init_data.get("url") or init_data.get("uploadUrl")
            media_id = init_data.get("id") or init_data.get("mediaId")
            
            # Шаг 2: Загрузить файл
            logging.info(f"Spotify Creators: Загрузка {os.path.basename(video_path)}...")
            with open(video_path, "rb") as f:
                up = requests.put(upload_url, data=f,
                                  headers={"Content-Type": "video/mp4",
                                           "Content-Length": str(os.path.getsize(video_path))})
            if up.status_code not in [200, 201, 204]:
                raise Exception(f"Ошибка загрузки файла: {up.text}")
            
            # Шаг 3: Создать эпизод
            ep_resp = self._req("post", f"{self.CREATORS_API}/shows/{self.show_id}/episodes",
                                json={"showId": self.show_id, "title": title,
                                      "description": description, "contentType": "video",
                                      "mediaId": media_id, "publishedAt": None})
            if ep_resp.status_code not in [200, 201]:
                raise Exception(f"Ошибка создания эпизода: {ep_resp.text}")
            
            ep = ep_resp.json()
            ep_id = ep.get("id") or ep.get("episodeId")
            ep_url = f"https://creators.spotify.com/pod/show/{self.show_id}/episodes/{ep_id}"
            logging.info(f"Spotify Creators: Загружено успешно - {ep_url}")
            return {"success": True, "episode_id": ep_id, "url": ep_url}
        
        except Exception as e:
            logging.error(f"Spotify Creators: Ошибка - {e}")
            return {"success": False, "error": str(e)}
# =============================================================================
# BOOSTY UPLOADER
# =============================================================================

class BoostyUploader:
    def __init__(self, access_token, blog_name):
        self.access_token = access_token
        self.blog_name = blog_name
        self.base_url = "https://api.boosty.to/v1"
        logging.info("Boosty: Инициализация")
    
    def upload_video(self, video_path, title, description):
        try:
            headers = {
                'Authorization': f'Bearer {self.access_token}',
            }
            
            # Шаг 1: Получить URL для загрузки видео
            logging.info("Boosty: Запрос URL для загрузки...")
            upload_request = requests.post(
                f'{self.base_url}/blog/{self.blog_name}/media/video',
                headers=headers
            )
            
            if upload_request.status_code != 200:
                raise Exception(f"Ошибка получения URL: {upload_request.text}")
            
            upload_data = upload_request.json()
            upload_url = upload_data['url']
            video_id = upload_data['id']
            
            logging.info(f"Boosty: Загрузка видео ID: {video_id}")
            
            # Шаг 2: Загрузить видеофайл
            with open(video_path, 'rb') as video_file:
                files = {'file': video_file}
                upload_response = requests.post(upload_url, files=files)
            
            if upload_response.status_code != 200:
                raise Exception(f"Ошибка загрузки файла: {upload_response.text}")
            
            # Шаг 3: Создать пост с видео
            logging.info("Boosty: Создание поста...")
            post_data = {
                'title': title,
                'data': [
                    {
                        'type': 'text',
                        'content': description,
                        'modificator': 'PLAIN'
                    },
                    {
                        'type': 'video',
                        'id': video_id
                    }
                ],
                'price': CONFIG['boosty']['price'],
                'teaser': CONFIG['boosty']['teaser'] or description[:200]
            }
            
            post_response = requests.post(
                f'{self.base_url}/blog/{self.blog_name}/post/',
                headers=headers,
                json=post_data
            )
            
            if post_response.status_code not in [200, 201]:
                raise Exception(f"Ошибка создания поста: {post_response.text}")
            
            post_id = post_response.json()['id']
            post_url = f"https://boosty.to/{self.blog_name}/posts/{post_id}"
            
            logging.info(f"Boosty: Пост создан успешно - {post_url}")
            return {'success': True, 'post_id': post_id, 'url': post_url}
            
        except Exception as e:
            logging.error(f"Boosty: Ошибка загрузки - {str(e)}")
            return {'success': False, 'error': str(e)}

# =============================================================================
# PATREON UPLOADER
# =============================================================================

class PatreonUploader:
    def __init__(self, access_token, campaign_id):
        self.access_token = access_token
        self.campaign_id = campaign_id
        self.base_url = "https://www.patreon.com/api/oauth2/v2"
        logging.info("Patreon: Инициализация")
    
    def upload_video(self, video_path, title, description):
        try:
            headers = {
                'Authorization': f'Bearer {self.access_token}',
                'Content-Type': 'application/json'
            }
            
            # Шаг 1: Загрузить видео (через сторонний хостинг или embed)
            # Patreon не имеет прямой загрузки видео через API
            # Вариант 1: Загрузить на YouTube и вставить embed
            # Вариант 2: Использовать внешний видеохостинг
            
            logging.info("Patreon: Создание поста...")
            
            # Формирование HTML с видео
            video_html = f"""
            <p>{description}</p>
            <p>Видео доступно для просмотра патронам.</p>
            """
            
            # Создание поста
            post_data = {
                'data': {
                    'type': 'post',
                    'attributes': {
                        'title': title,
                        'content': video_html,
                        'is_paid': CONFIG['patreon']['is_paid'],
                        'is_public': not CONFIG['patreon']['is_paid'],
                    },
                    'relationships': {
                        'campaign': {
                            'data': {
                                'type': 'campaign',
                                'id': self.campaign_id
                            }
                        }
                    }
                }
            }
            
            # Добавить ограничение по tier, если указано
            if CONFIG['patreon']['tier_ids']:
                post_data['data']['relationships']['tiers'] = {
                    'data': [
                        {'type': 'tier', 'id': tier_id} 
                        for tier_id in CONFIG['patreon']['tier_ids']
                    ]
                }
            
            response = requests.post(
                f'{self.base_url}/posts',
                headers=headers,
                json=post_data
            )
            
            if response.status_code not in [200, 201]:
                raise Exception(f"Ошибка создания поста: {response.text}")
            
            post_id = response.json()['data']['id']
            post_url = f"https://www.patreon.com/posts/{post_id}"
            
            logging.info(f"Patreon: Пост создан успешно - {post_url}")
            logging.warning("Patreon: Видео нужно загрузить отдельно (API ограничен)")
            
            return {'success': True, 'post_id': post_id, 'url': post_url, 
                    'note': 'Видео требует ручной загрузки или embed из YouTube'}
            
        except Exception as e:
            logging.error(f"Patreon: Ошибка загрузки - {str(e)}")
            return {'success': False, 'error': str(e)}

# =============================================================================
# FILE WATCHER
# =============================================================================

class VideoUploadHandler(FileSystemEventHandler):
    def __init__(self, youtube_uploader, spotify_uploader, boosty_uploader, patreon_uploader):
        self.youtube_uploader = youtube_uploader
        self.spotify_uploader = spotify_uploader
        self.boosty_uploader = boosty_uploader
        self.patreon_uploader = patreon_uploader
        self.processing = set()
    
    def on_created(self, event):
        if event.is_directory:
            return
        
        file_path = event.src_path
        file_ext = os.path.splitext(file_path)[1].lower()
        
        if file_ext not in CONFIG['video_extensions']:
            return
        
        if file_path in self.processing:
            return
        
        time.sleep(5)
        
        logging.info(f"Обнаружен новый файл: {file_path}")
        self.processing.add(file_path)
        self.process_video(file_path)
        self.processing.remove(file_path)
    
    def process_video(self, video_path):
        video_name = os.path.basename(video_path)
        base_name = os.path.splitext(video_name)[0]
        
        # Ищем файл с описанием
        description_file = os.path.join(
            os.path.dirname(video_path),
            f"{base_name}.txt"
        )
        
        description = ""
        if os.path.exists(description_file):
            with open(description_file, 'r', encoding='utf-8') as f:
                description = f.read()
            logging.info(f"Найдено описание в файле: {description_file}")
        
        title = base_name.replace('_', ' ').replace('-', ' ')
        
        logging.info(f"Начинается загрузка: {title}")
        
        # Загрузка на все платформы
        youtube_result = self._upload_with_retry(
            lambda: self.youtube_uploader.upload_video(video_path, title, description),
            "YouTube"
        )
        
        spotify_result = self._upload_with_retry(
            lambda: self.spotify_uploader.upload_video(video_path, title, description),
            "Spotify"
        )
        
        boosty_result = self._upload_with_retry(
            lambda: self.boosty_uploader.upload_video(video_path, title, description),
            "Boosty"
        )
        
        patreon_result = self._upload_with_retry(
            lambda: self.patreon_uploader.upload_video(video_path, title, description),
            "Patreon"
        )
        
        # Перемещение обработанного файла
        all_success = all([
            youtube_result['success'],
            spotify_result['success'],
            boosty_result['success'],
            patreon_result['success']
        ])
        
        if all_success:
            self._move_to_processed(video_path)
            if os.path.exists(description_file):
                self._move_to_processed(description_file)
            
            self._save_upload_results(
                video_name, youtube_result, spotify_result, 
                boosty_result, patreon_result
            )
    
    def _upload_with_retry(self, upload_func, platform_name):
        for attempt in range(1, CONFIG['max_retries'] + 1):
            logging.info(f"{platform_name}: Попытка {attempt}/{CONFIG['max_retries']}")
            result = upload_func()
            
            if result['success']:
                return result
            
            if attempt < CONFIG['max_retries']:
                logging.warning(f"{platform_name}: Ошибка, повтор через {CONFIG['retry_delay']} сек")
                time.sleep(CONFIG['retry_delay'])
        
        return result
    
    def _move_to_processed(self, file_path):
        processed_path = os.path.join(
            CONFIG['processed_folder'],
            os.path.basename(file_path)
        )
        os.makedirs(CONFIG['processed_folder'], exist_ok=True)
        os.rename(file_path, processed_path)
        logging.info(f"Файл перемещен в обработанные: {processed_path}")
    
    def _save_upload_results(self, video_name, youtube_result, spotify_result, 
                            boosty_result, patreon_result):
        results = {
            'video': video_name,
            'timestamp': datetime.now().isoformat(),
            'youtube': youtube_result,
            'spotify': spotify_result,
            'boosty': boosty_result,
            'patreon': patreon_result,
        }
        
        results_file = 'upload_results_full.json'
        
        if os.path.exists(results_file):
            with open(results_file, 'r', encoding='utf-8') as f:
                all_results = json.load(f)
        else:
            all_results = []
        
        all_results.append(results)
        
        with open(results_file, 'w', encoding='utf-8') as f:
            json.dump(all_results, f, indent=2, ensure_ascii=False)

# =============================================================================
# MAIN
# =============================================================================

def main():
    logging.info("=" * 60)
    logging.info("Запуск автозагрузчика видео на YouTube, Spotify, Boosty, Patreon")
    logging.info("=" * 60)
    
    # Создание папок
    os.makedirs(CONFIG['watch_folder'], exist_ok=True)
    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    
    # Инициализация загрузчиков
    youtube_uploader = YouTubeUploader(CONFIG['youtube']['client_secrets_file'])
    spotify_uploader = SpotifyUploader(
        CONFIG['spotify']['client_id'],
        CONFIG['spotify']['client_secret'],
        CONFIG['spotify']['access_token'],
        CONFIG['spotify']['refresh_token']
    )
    boosty_uploader = BoostyUploader(
        CONFIG['boosty']['access_token'],
        CONFIG['boosty']['blog_name']
    )
    patreon_uploader = PatreonUploader(
        CONFIG['patreon']['access_token'],
        CONFIG['patreon']['campaign_id']
    )
    
    # Настройка мониторинга
    event_handler = VideoUploadHandler(
        youtube_uploader, spotify_uploader,
        boosty_uploader, patreon_uploader
    )
    observer = Observer()
    observer.schedule(event_handler, CONFIG['watch_folder'], recursive=False)
    observer.start()
    
    logging.info(f"Мониторинг папки: {CONFIG['watch_folder']}")
    logging.info("Для остановки нажмите Ctrl+C")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
        logging.info("Остановка мониторинга...")
    
    observer.join()
    logging.info("Работа завершена")

if __name__ == '__main__':
    main()