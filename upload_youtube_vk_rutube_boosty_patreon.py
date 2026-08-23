"""
Скрипт для автоматической загрузки видео на YouTube, VK, Rutube, Boosty и Patreon
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
import vk_api
from vk_api import VkUpload

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('upload_log_full_ru.txt', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# =============================================================================
# КОНФИГУРАЦИЯ - НАСТРОЙТЕ ЭТИ ПАРАМЕТРЫ
# =============================================================================

CONFIG = {
    # Папка для мониторинга
    'watch_folder': './videos_to_upload_ru',
    
    # Папка для обработанных видео
    'processed_folder': './processed_videos_ru',
    
    # YouTube настройки
    'youtube': {
        'client_secrets_file': 'client_secret.json',
        'privacy_status': 'public',
        'category_id': '22',
    },
    
    # ВКонтакте настройки
    'vk': {
        'access_token': 'YOUR_VK_ACCESS_TOKEN',
        'group_id': 0,
        'is_private': 0,
    },
    
    # Rutube настройки
    'rutube': {
        'client_id': 'YOUR_RUTUBE_CLIENT_ID',
        'client_secret': 'YOUR_RUTUBE_CLIENT_SECRET',
        'access_token': 'YOUR_RUTUBE_ACCESS_TOKEN',
        'is_hidden': False,
        'category_id': 24,
    },
    
    # Boosty настройки
    'boosty': {
        'access_token': 'YOUR_BOOSTY_ACCESS_TOKEN',
        'blog_name': 'your_blog_name',
        'price': 0,
        'teaser': '',
    },
    
    # Patreon настройки
    'patreon': {
        'access_token': 'YOUR_PATREON_ACCESS_TOKEN',
        'campaign_id': 'YOUR_CAMPAIGN_ID',
        'tier_ids': [],
        'is_paid': False,
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
# VK UPLOADER
# =============================================================================

class VKUploader:
    def __init__(self, access_token, group_id=0):
        self.access_token = access_token
        self.group_id = group_id
        self.vk_session = vk_api.VkApi(token=access_token)
        self.vk = self.vk_session.get_api()
        self.upload = VkUpload(self.vk_session)
        logging.info("VK: Аутентификация успешна")
    
    def upload_video(self, video_path, title, description):
        try:
            logging.info("VK: Начало загрузки видео...")
            
            if self.group_id > 0:
                video = self.upload.video(
                    video_file=video_path,
                    name=title,
                    description=description,
                    group_id=self.group_id,
                    is_private=CONFIG['vk']['is_private']
                )
            else:
                video = self.upload.video(
                    video_file=video_path,
                    name=title,
                    description=description,
                    is_private=CONFIG['vk']['is_private']
                )
            
            video_id = video[0]['video_id']
            owner_id = video[0]['owner_id']
            video_url = f"https://vk.com/video{owner_id}_{video_id}"
            
            logging.info(f"VK: Видео загружено успешно - {video_url}")
            return {'success': True, 'video_id': video_id, 'url': video_url}
            
        except Exception as e:
            logging.error(f"VK: Ошибка загрузки - {str(e)}")
            return {'success': False, 'error': str(e)}

# =============================================================================
# RUTUBE UPLOADER
# =============================================================================

class RutubeUploader:
    def __init__(self, client_id, client_secret, access_token):
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.base_url = "https://rutube.ru/api"
        logging.info("Rutube: Инициализация")
    
    def upload_video(self, video_path, title, description):
        try:
            headers = {
                'Authorization': f'Bearer {self.access_token}',
            }
            
            create_response = requests.post(
                f'{self.base_url}/video/',
                headers=headers,
                json={
                    'title': title,
                    'description': description,
                    'is_hidden': CONFIG['rutube']['is_hidden'],
                    'category_id': CONFIG['rutube']['category_id'],
                }
            )
            
            if create_response.status_code not in [200, 201]:
                raise Exception(f"Ошибка создания видео: {create_response.text}")
            
            video_data = create_response.json()
            video_id = video_data['id']
            upload_url = video_data['video_url']
            
            logging.info(f"Rutube: Создан видео-объект ID: {video_id}")
            
            with open(video_path, 'rb') as video_file:
                upload_response = requests.put(
                    upload_url,
                    data=video_file,
                    headers={'Content-Type': 'application/octet-stream'}
                )
            
            if upload_response.status_code != 200:
                raise Exception(f"Ошибка загрузки файла: {upload_response.text}")
            
            video_url = f"https://rutube.ru/video/{video_id}/"
            logging.info(f"Rutube: Видео загружено успешно - {video_url}")
            
            return {'success': True, 'video_id': video_id, 'url': video_url}
            
        except Exception as e:
            logging.error(f"Rutube: Ошибка загрузки - {str(e)}")
            return {'success': False, 'error': str(e)}

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
            
            with open(video_path, 'rb') as video_file:
                files = {'file': video_file}
                upload_response = requests.post(upload_url, files=files)
            
            if upload_response.status_code != 200:
                raise Exception(f"Ошибка загрузки файла: {upload_response.text}")
            
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
            
            logging.info("Patreon: Создание поста...")
            
            video_html = f"""
            <p>{description}</p>
            <p>Видео доступно для просмотра патронам.</p>
            """
            
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
    def __init__(self, youtube_uploader, vk_uploader, rutube_uploader, 
                 boosty_uploader, patreon_uploader):
        self.youtube_uploader = youtube_uploader
        self.vk_uploader = vk_uploader
        self.rutube_uploader = rutube_uploader
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
        
        vk_result = self._upload_with_retry(
            lambda: self.vk_uploader.upload_video(video_path, title, description),
            "VK"
        )
        
        rutube_result = self._upload_with_retry(
            lambda: self.rutube_uploader.upload_video(video_path, title, description),
            "Rutube"
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
            vk_result['success'],
            rutube_result['success'],
            boosty_result['success'],
            patreon_result['success']
        ])
        
        if all_success:
            self._move_to_processed(video_path)
            if os.path.exists(description_file):
                self._move_to_processed(description_file)
            
            self._save_upload_results(
                video_name, youtube_result, vk_result, rutube_result,
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
    
    def _save_upload_results(self, video_name, youtube_result, vk_result, 
                            rutube_result, boosty_result, patreon_result):
        results = {
            'video': video_name,
            'timestamp': datetime.now().isoformat(),
            'youtube': youtube_result,
            'vk': vk_result,
            'rutube': rutube_result,
            'boosty': boosty_result,
            'patreon': patreon_result,
        }
        
        results_file = 'upload_results_full_ru.json'
        
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
    logging.info("=" * 70)
    logging.info("Запуск автозагрузчика на YouTube, VK, Rutube, Boosty, Patreon")
    logging.info("=" * 70)
    
    os.makedirs(CONFIG['watch_folder'], exist_ok=True)
    os.makedirs(CONFIG['processed_folder'], exist_ok=True)
    
    youtube_uploader = YouTubeUploader(CONFIG['youtube']['client_secrets_file'])
    vk_uploader = VKUploader(
        CONFIG['vk']['access_token'],
        CONFIG['vk']['group_id']
    )
    rutube_uploader = RutubeUploader(
        CONFIG['rutube']['client_id'],
        CONFIG['rutube']['client_secret'],
        CONFIG['rutube']['access_token']
    )
    boosty_uploader = BoostyUploader(
        CONFIG['boosty']['access_token'],
        CONFIG['boosty']['blog_name']
    )
    patreon_uploader = PatreonUploader(
        CONFIG['patreon']['access_token'],
        CONFIG['patreon']['campaign_id']
    )
    
    event_handler = VideoUploadHandler(
        youtube_uploader, vk_uploader, rutube_uploader,
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
