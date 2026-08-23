# from google_auth_oauthlib.flow import InstalledAppFlow
# from googleapiclient.discovery import build

# SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

# # Авторизация
# flow = InstalledAppFlow.from_client_secrets_file(
#     'client_secret_521669452390-j4s4h535sv21h355aobn0vpa6o6ebl1a.apps.googleusercontent.com.json', 
#     SCOPES
# )
# creds = flow.run_local_server(port=0)  # port=0 = автоматический выбор порта

# # Подключение к YouTube API
# youtube = build('youtube', 'v3', credentials=creds)

# print("✅ Успешно подключено к YouTube API!")
# print(f"Канал: {youtube.channels().list(part='snippet', mine=True).execute()['items'][0]['snippet']['title']}")

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ['https://www.googleapis.com/auth/youtube.upload']

flow = InstalledAppFlow.from_client_secrets_file(
    'client_secret_youtuberu.json', 
    SCOPES,
    redirect_uri='http://localhost:8080/'
)

# Вместо браузера - консольная авторизация
print("Откройте эту ссылку в браузере:")
auth_url, _ = flow.authorization_url(prompt='consent')
print(auth_url)
print()

code = input("Вставьте код из URL после авторизации: ")
flow.fetch_token(code=code)

print("✅ Авторизация успешна!")
