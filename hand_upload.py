import pickle
from googleapiclient.discovery import build

# Загрузите токен
with open('youtube_token.pickle', 'rb') as f:
    creds = pickle.load(f)

youtube = build('youtube', 'v3', credentials=creds)

# Попробуйте добавить вручную
try:
    result = youtube.playlistItems().insert(
        part="snippet",
        body={
            'snippet': {
                'playlistId': 'PLgJoFV4tOm9OHGvu8XuFH_aEFna9oXjcZ',
                'resourceId': {
                    'kind': 'youtube#video',
                    'videoId': 'mnYrkTHbasM'  # ID только что загруженного видео
                }
            }
        }
    ).execute()
    print("✅ Успешно добавлено!")
    print(result)
except Exception as e:
    print(f"❌ Ошибка: {e}")