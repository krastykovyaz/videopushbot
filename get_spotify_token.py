

import webbrowser
import urllib.parse
import requests
import json
import base64
import os

# =============================================
# ВАШИ ДАННЫЕ
# =============================================
CLIENT_ID     = "e193779f228e4d16b5428ea170741f62"
CLIENT_SECRET = "bbb8fc6696a540d696bb3ffc067282ad"   # Dashboard → Settings → View client secret
REDIRECT_URI  = "https://creators.spotify.com/pod/dashboard/home"

# =============================================
# SCOPES — только реально существующие
# podcast-write / podcast-read НЕ существуют в Spotify OAuth → "Illegal scope"
# Creators API авторизуется только через user-read-private + email
# =============================================
SCOPES = "user-read-private user-read-email"

# =============================================

def get_auth_url():
    params = {
        "client_id":     CLIENT_ID,
        "response_type": "code",
        "redirect_uri":  REDIRECT_URI,
        "scope":         SCOPES,
        "show_dialog":   "true",
    }
    return "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode(params)


def exchange_code(auth_code):
    creds = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
    resp = requests.post(
        "https://accounts.spotify.com/api/token",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type":  "application/x-www-form-urlencoded",
        },
        data={
            "grant_type":   "authorization_code",
            "code":         auth_code,
            "redirect_uri": REDIRECT_URI,
        },
    )
    return resp.json() if resp.status_code == 200 else None


def main():
    print("=" * 60)
    print("  Spotify Creators — получение токена")
    print("=" * 60)

    # ── проверка секрета ──────────────────────────────────────
    if "ВСТАВЬТЕ" in CLIENT_SECRET:
        print()
        print("❌  Вставьте Client Secret!")
        print()
        print("  1. Откройте https://developer.spotify.com/dashboard")
        print("  2. Выберите приложение 'my pushs'")
        print("  3. Нажмите Settings")
        print("  4. Нажмите 'View client secret'")
        print("  5. Скопируйте значение в переменную CLIENT_SECRET выше")
        return

    # ── открываем браузер ─────────────────────────────────────
    url = get_auth_url()
    print()
    print("Шаг 1.  Открываем браузер для авторизации...")
    print()
    print(url)
    print()
    webbrowser.open(url)

    print("Шаг 2.  Войдите в Spotify и нажмите 'Agree'.")
    print()
    print("Шаг 3.  Вас перенаправит на страницу Spotify Creators.")
    print("        Скопируйте значение параметра 'code' из адресной строки.")
    print()
    print("        Пример URL:")
    print("        https://creators.spotify.com/pod/dashboard/home?code=AQD...")
    print("                                                              ^^^^")
    print("        Нужно ЭТО значение (только code, без остального)")
    print()

    raw = input("Вставьте code из URL: ").strip()
    if not raw:
        print("❌  Код не введён.")
        return

    # вырезаем code= если пользователь вставил весь URL
    if "code=" in raw:
        raw = raw.split("code=")[1].split("&")[0]

    print()
    print("Обмениваем code на токен...")
    data = exchange_code(raw)

    if not data or "access_token" not in data:
        print(f"❌  Ошибка: {data}")
        print()
        print("Возможные причины:")
        print("  • Неверный Client Secret")
        print("  • Code уже использован (каждый code одноразовый — начните заново)")
        print("  • Redirect URI не совпадает с настройками в Dashboard")
        return

    # ── сохраняем ─────────────────────────────────────────────
    with open("spotify_tokens.json", "w") as f:
        json.dump(data, f, indent=2)

    print()
    print("✅  ТОКЕН ПОЛУЧЕН!")
    print()
    print("  access_token  :", data["access_token"][:40], "...")
    print("  refresh_token :", data.get("refresh_token", "нет")[:40], "...")
    print("  expires_in    :", data.get("expires_in"), "сек (~1 час)")
    print("  scope         :", data.get("scope"))
    print()
    print("  Сохранено в spotify_tokens.json")
    print()
    print("  Теперь запускайте основной скрипт — токен подхватится автоматически.")


if __name__ == "__main__":
    main()