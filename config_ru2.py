import os
from dotenv import load_dotenv

load_dotenv()

CONFIG = {
    # Telegram API credentials
    'api_id': int(os.getenv('API_ID')),
    'api_hash': os.getenv('API_HASH'),
    'phone_number': os.getenv('PHONE_NUMBER'),

    # Папки
    'download_folder': os.getenv('WATCH_FOLDER', './videos_to_upload'),
    'processed_folder': os.getenv('PROCESSED_FOLDER', './uploaded_videos'),

    # Разрешённые пользователи
    'allowed_users': list(map(int, os.getenv('ALLOWED_USERS').split(','))),

    # Типы файлов
    'video_extensions': ['.mp4', '.mov', '.avi', '.mkv', '.webm'],
    'document_extensions': ['.pdf', '.txt', '.md'],

    # Gemini
    'gemini': {
        'api_key': os.getenv("API_KEY"),
        'model': os.getenv("MODEL_RU"),
    },

    # YouTube плейлисты (RU канал)
    'playlists': {
        'AI Paper Review':         os.getenv("AI_Paper_Review_Ru"),
        'Crypto Ideas':            os.getenv("Crypto_Ideas_Ru"),
        'GPMorgan report debates': os.getenv("GPMorgan_report_debates_Ru"),
        'The Economist':           os.getenv("The_Economist_Ru"),
        'The National Geo talks':  os.getenv("The_National_Geo_talks_Ru"),
        'Auto Detail':             os.getenv("Auto_Detail_Ru"),
    },

    # YouTube настройки
    'youtube': {
        'client_secrets_file': os.getenv("SECRET_YOUTUBE_FILE_Ru"),
        'privacy_status': 'private',
        'category_id': '22',
        'auto_upload': True,
    },

    # VK настройки
    # owner_id — отрицательный ID группы (-club_id)
    'vk': {
        'access_token': os.getenv("VK_ACCESS_TOKEN"),
        'channels': {
            'AI Paper Review':         -230606581,
            'The Economist':           -230643441,
            'GPMorgan report debates': -230907329,
            'The National Geo talks':  -230969252,
            'Crypto Ideas':            -234544838,
            'Auto Detail':             -231412734,
        }
    },

    'default_thumbnail': 'unnamed.png',
    'max_file_size': 2 * 1024 * 1024 * 1024,  # 2 GB
}