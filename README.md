# NotebookLM Clone — Telegram Userbot

Принимает PDF через Telegram, отдаёт:
- 🎬 Видео MP4 ~7 минут (озвучка двух ведущих + картинки из статьи)
- 🖼 Thumbnail PNG в стиле YouTube

**Полностью бесплатно** при 3–5 видео/день.

---

## Быстрый старт

### 1. Получить Telegram API credentials
Идти на https://my.telegram.org → "API development tools" → создать приложение.
Скопировать `api_id` и `api_hash`.

### 2. Получить Gemini API key (бесплатно)
https://aistudio.google.com/app/apikey → "Create API key"

### 3. Установить зависимости

```bash
# Системные
apt install ffmpeg   # Ubuntu/Debian
# brew install ffmpeg  # macOS

# Python
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt

# Если нет GPU — установить CPU-версию torch:
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
```

### 4. Настроить .env

```bash
cp .env.example .env
# Заполнить API_ID, API_HASH, PHONE_NUMBER, GEMINI_API_KEY
```

### 5. Запустить

```bash
python bot.py
```

При первом запуске Telegram попросит ввести код из SMS — это нормально.
После авторизации создастся файл `notebooklm_session.session` — не удалять.

---

## Структура проекта

```
notebooklm/
├── bot.py                    # Telethon userbot, входная точка
├── queue_worker.py           # Очередь задач (asyncio)
├── pipeline/
│   ├── step01_extract.py     # PDF → текст + картинки
│   ├── step02_script.py      # LLM → скрипт диалога
│   ├── step03_tts.py         # Chatterbox → WAV + timeline
│   ├── step04_frames.py      # Pillow → PNG кадры 1920x1080
│   ├── step05_video.py       # ffmpeg → MP4
│   └── step06_thumbnail.py   # Pillow → thumbnail 1280x720
├── workspace/                # Временные файлы (создаётся автоматически)
│   └── {user_id}/{job_id}/
│       ├── input.pdf
│       ├── extracted/
│       │   ├── text_blocks.json
│       │   └── images/
│       ├── script.json
│       ├── audio/
│       ├── frames/
│       ├── timeline.json
│       ├── final_audio.mp3
│       ├── output_video.mp4
│       └── thumbnail.png
├── .env
├── .env.example
└── requirements.txt
```

---

## Переменные окружения

| Переменная | Описание | Пример |
|---|---|---|
| `API_ID` | Telegram API ID | `12345678` |
| `API_HASH` | Telegram API Hash | `abc123...` |
| `PHONE_NUMBER` | Твой номер телефона | `+79001234567` |
| `GEMINI_API_KEY` | Google AI Studio key | `AIza...` |
| `DEFAULT_LANG` | Язык по умолчанию | `ru` или `en` |
| `MAX_WORKERS` | Параллельных задач | `1` (рекомендуется) |
| `ALLOWED_USERS` | Telegram user_id через запятую | пусто = все |
| `VOICE_HOST1_REF` | Путь к WAV для клонирования голоса Host1 | `voices/host1.wav` |
| `VOICE_HOST2_REF` | Путь к WAV для клонирования голоса Host2 | `voices/host2.wav` |
| `WORKSPACE_DIR` | Папка для временных файлов | `./workspace` |

---

## Команды в Telegram

| Команда | Действие |
|---|---|
| `/start` или `/help` | Приветствие и инструкция |
| `/lang ru` | Установить русский язык |
| `/lang en` | Установить английский язык |
| `/status` | Позиция в очереди |
| Отправить PDF | Запустить генерацию |

---

## Клонирование голоса (опционально)

Chatterbox поддерживает zero-shot voice cloning.
Положи 5–10 секунд чистой речи в WAV (16kHz моно) в папку `voices/`
и укажи путь в `.env`:

```
VOICE_HOST1_REF=voices/host1.wav
VOICE_HOST2_REF=voices/host2.wav
```

---

## Советы по производительности

**Без GPU (CPU):**
- Генерация 7 мин аудио ≈ 5–10 минут
- MAX_WORKERS=1 обязательно

**С GPU (NVIDIA):**
- Генерация 7 мин аудио ≈ 30–60 секунд
- Можно попробовать MAX_WORKERS=2

**Google Colab (бесплатный GPU T4):**
- Запустить bot.py в Colab с ngrok или просто как скрипт
- Tesla T4 ускоряет TTS в ~10 раз по сравнению с CPU

---

## Стоимость (3–5 видео/день)

| Компонент | Инструмент | Стоимость |
|---|---|---|
| LLM (скрипт) | Gemini 2.5 Flash free tier | $0 |
| TTS | Chatterbox (локально) | $0 |
| Видео рендер | ffmpeg | $0 |
| Изображения | Из самого PDF | $0 |
| **Итого** | | **$0/мес** |
