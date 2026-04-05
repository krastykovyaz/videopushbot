import os
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    # Telegram API credentials
    'api_id': int(os.getenv('API_ID')),
    'api_hash': os.getenv('API_HASH'),
    'phone_number': os.getenv('PHONE_NUMBER'),
    
    # Папка для сохранения файлов
    'download_folder': os.getenv('WATCH_FOLDER'),
    'processed_folder': os.getenv('PROCESSED_FOLDER'),
    
    # Разрешённые пользователи
    'allowed_users': list(map(int, os.getenv('ALLOWED_USERS').split(','))),
    
    # Поддерживаемые типы файлов
    'video_extensions': ['.mp4', '.mov', '.avi', '.mkv', '.webm'],
    'document_extensions': ['.pdf', '.txt', '.md'],
    
    # Gemini API
    'gemini': {
        'api_key':  os.getenv("API_KEY"),
        'model': os.getenv("MODEL_RU"),
    },
    
    # Плейлисты
    'playlists': {
        'AI Paper Review':  os.getenv("AI_Paper_Review_Ru"),
        'Crypto Ideas':  os.getenv("Crypto_Ideas_Ru"),
        'GPMorgan report debates':  os.getenv("GPMorgan_report_debates_Ru"),
        'The Economist':  os.getenv("The_Economist_Ru"),
        'The National Geo talks':  os.getenv("The_National_Geo_talks_Ru"),
        'Auto Detail':  os.getenv("Auto_Detail_Ru")
    },
    
    # YouTube настройки
    'youtube': {
        'client_secrets_file': os.getenv("SECRET_YOUTUBE_FILE_Ru"),
        'privacy_status': 'private',  # 'public', 'private', 'unlisted'
        'category_id': '22',
        'auto_upload': True,  # Автоматическая загрузка видео на YouTube
    },
    
    'default_thumbnail': 'unnamed.png',
    'max_file_size': 2 * 1024 * 1024 * 1024,  # 2 GB
}