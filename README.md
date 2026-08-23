# Автоматические загрузчики видео

Два Python-скрипта для автоматической загрузки видео на различные платформы с мониторингом папок.

## 📦 Скрипты

### 1. `upload_youtube_spotify_boosty_patreon.py`
Загружает видео на **YouTube**, **Spotify Podcasters**, **Boosty** и **Patreon**

### 2. `upload_youtube_vk_rutube_boosty_patreon.py`
Загружает видео на **YouTube**, **ВКонтакте**, **Rutube**, **Boosty** и **Patreon**

## 🚀 Установка

### Шаг 1: Установить Python
Убедитесь, что у вас установлен Python 3.8 или новее:
```bash
python --version
```

### Шаг 2: Установить зависимости
```bash
pip install -r requirements.txt
```

## ⚙️ Настройка

### YouTube (для обоих скриптов)

1. Перейдите в [Google Cloud Console](https://console.cloud.google.com/)
2. Создайте новый проект
3. Включите **YouTube Data API v3**
4. Создайте OAuth 2.0 credentials
5. Скачайте файл `client_secret.json` и поместите в папку со скриптом
6. При первом запуске откроется браузер для авторизации

### VK (для второго скрипта)

1. Перейдите в [VK для разработчиков](https://vk.com/apps?act=manage)
2. Создайте Standalone-приложение
3. Получите токен доступа с правами: `video, offline`
4. Вставьте токен в `CONFIG['vk']['access_token']`

Для получения токена можно использовать:
```python
import vk_api

vk_session = vk_api.VkApi('+71234567890', 'your_password')
vk_session.auth()
```

### Rutube (для второго скрипта)

1. Зарегистрируйтесь на [Rutube](https://rutube.ru/)
2. Перейдите в [настройки API](https://rutube.ru/api/)
3. Создайте приложение и получите:
   - `client_id`
   - `client_secret`
   - `access_token`
4. Вставьте данные в `CONFIG['rutube']`

### Boosty (для обоих скриптов)

1. Зайдите на [Boosty](https://boosty.to/)
2. Войдите в свой аккаунт
3. Получите токен доступа:
   - Через DevTools (F12) → Application → Cookies → найдите 'auth'
   - Или через официальное API (если доступно)
4. Найдите имя вашего блога (из URL: `boosty.to/your_blog_name`)
5. Вставьте в `CONFIG['boosty']`

**Подробная инструкция:** см. файл `BOOSTY_PATREON_GUIDE.md`

### Patreon (для обоих скриптов)

1. Создайте приложение на [Patreon Portal](https://www.patreon.com/portal/registration/register-clients)
2. Получите Client ID и Client Secret
3. Получите Access Token через OAuth flow
4. Получите Campaign ID через API
5. Опционально: получите Tier IDs для ограничения доступа
6. Вставьте в `CONFIG['patreon']`

**⚠️ Важно:** Patreon не поддерживает прямую загрузку видео через API. Видео нужно загружать на YouTube (unlisted) и вставлять embed, или использовать внешний хостинг.

**Подробная инструкция:** см. файл `BOOSTY_PATREON_GUIDE.md`

### Spotify Podcasters (для первого скрипта)

**Важно:** Spotify не предоставляет публичное API для загрузки видео. Варианты:

1. **Anchor API** (если у вас есть доступ):
   - Получите API ключ на anchor.fm
   - Вставьте в `CONFIG['spotify']['api_key']`

2. **Альтернатива**: Используйте автоматизацию браузера (Selenium)

Или замените на другую платформу (TikTok, Telegram).

## 📝 Использование

### Запуск первого скрипта (YouTube + Spotify + Boosty + Patreon)

```bash
python upload_youtube_spotify_boosty_patreon.py
```

### Запуск второго скрипта (YouTube + VK + Rutube + Boosty + Patreon)

```bash
python upload_youtube_vk_rutube_boosty_patreon.py
```

### Структура файлов

Положите видео и описание в папку для мониторинга:

```
videos_to_upload/
├── my_video.mp4          # Видеофайл
└── my_video.txt          # Описание (опционально)
```

**Формат описания** (`my_video.txt`):
```
Это описание моего видео.
Может быть многострочным.

Поддерживаются переносы строк.
```

**Поддерживаемые форматы видео:**
- `.mp4` (рекомендуется)
- `.mov`
- `.avi`
- `.mkv`
- `.flv`

## 🔧 Настройка конфигурации

Отредактируйте раздел `CONFIG` в начале каждого скрипта:

```python
CONFIG = {
    'watch_folder': './videos_to_upload',      # Папка для мониторинга
    'processed_folder': './processed_videos',   # Папка для обработанных
    
    'youtube': {
        'privacy_status': 'private',  # 'public', 'private', 'unlisted'
        'category_id': '22',          # Категория видео
    },
    
    'max_retries': 3,      # Количество попыток при ошибке
    'retry_delay': 60,     # Задержка между попытками (секунды)
}
```

## 📊 Логирование

Скрипты создают файлы логов:
- `upload_log.txt` - для первого скрипта
- `upload_log_vk_rutube.txt` - для второго скрипта

Результаты загрузки сохраняются в:
- `upload_results.json` - для первого скрипта
- `upload_results_ru.json` - для второго скрипта

## 🛠️ Устранение неполадок

### YouTube: "The request cannot be completed"
- Убедитесь, что YouTube Data API v3 включено
- Проверьте квоты API в Google Cloud Console

### VK: "Access denied"
- Проверьте права токена: `video, offline`
- Убедитесь, что токен не истек

### Rutube: 401 Unauthorized
- Обновите access token
- Проверьте правильность client_id и client_secret

### Файлы не загружаются
- Убедитесь, что файлы полностью скопированы (скрипт ждет 5 секунд)
- Проверьте формат видео (должен быть в списке поддерживаемых)
- Посмотрите логи для деталей ошибки

## 📋 Дополнительные функции

### Автозапуск при загрузке системы (Linux)

Создайте systemd service:
```bash
sudo nano /etc/systemd/system/video-uploader.service
```

Содержимое:
```ini
[Unit]
Description=Video Auto Uploader
After=network.target

[Service]
Type=simple
User=your_username
WorkingDirectory=/path/to/scripts
ExecStart=/usr/bin/python3 /path/to/scripts/upload_youtube_vk_rutube.py
Restart=always

[Install]
WantedBy=multi-user.target
```

Активировать:
```bash
sudo systemctl enable video-uploader.service
sudo systemctl start video-uploader.service
```

### Автозапуск (Windows)

Используйте Task Scheduler для запуска скрипта при входе в систему.

## 📄 Лицензия

MIT License - свободно используйте и модифицируйте под свои нужды.

## 📚 Дополнительные файлы

- **BOOSTY_PATREON_GUIDE.md** - Подробное руководство по настройке Boosty и Patreon
- **quick_start_guide.txt** - Краткая инструкция по быстрому старту
- **config_example.json** - Пример файла конфигурации

## 🤝 Поддержка

При возникновении проблем:
1. Проверьте логи
2. Убедитесь, что все токены и ключи API актуальны
3. Проверьте квоты API платформ

## ⚠️ Важные замечания

- **Квоты YouTube API:** 10,000 единиц в день (одна загрузка = ~1600 единиц)
- **Ограничения VK:** Зависят от типа аккаунта
- **Rutube:** Проверяйте лимиты на загрузку
- Храните токены и ключи в безопасности (не публикуйте в Git)
