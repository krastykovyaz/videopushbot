"""
Шаг 2: Генерация скрипта диалога двух ведущих через Gemini 2.5 Flash (free tier).
Каждый сегмент привязан к изображению из PDF.
"""

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

import google.generativeai as genai
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("step02")

genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Groq — фоллбэк если Gemini упал/исчерпан
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
GROQ_MODEL   = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")

PROMPTS = {
    "ru": """Ты — сценарист подкаста в стиле Google NotebookLM.
Напиши живой диалог двух ведущих (Host1 и Host2) по материалу статьи.

ПРАВИЛА:
- Длина: СТРОГО 3500–3800 слов. Это ~7 минут при русской озвучке. НЕ БОЛЬШЕ. Считай слова.
- Тон: умный, живой, как у лучших научпоп подкастов — Lex Fridman, Huberman Lab
- Host1 объясняет и углубляется в детали, Host2 задаёт острые вопросы и добавляет контекст
- Используй конкретные факты, цифры, примеры из статьи — не обобщай
- Добавляй естественные переходы: "кстати", "подожди, а это значит что...", "то есть получается"
- НЕ говори "как написано в статье", "авторы утверждают" — говори от себя как эксперт
- Host2 НЕ использует восклицания типа "Вот это да!", "Невероятно!", "Потрясающе!" — реагирует вдумчиво
- Каждая реплика Host2 либо задаёт конкретный вопрос, либо добавляет связанный факт или аналогию
- Реплики должны быть разной длины — часть короткие (1-2 предложения), часть развёрнутые
- Структура: введение (проблема) → метод → результаты → последствия → вывод
- ОБЯЗАТЕЛЬНО: последние 2 сегмента — прощание. Host1 подводит итог (2-3 предложения), Host2 прощается с аудиторией ("Спасибо что были с нами, до следующего раза!")

ФОРМАТ ОТВЕТА — строго JSON, без markdown:
{{
  "title": "заголовок темы (до 60 символов)",
  "key_points": ["тезис 1", "тезис 2", "тезис 3"],
  "segments": [
    {{
      "id": 1,
      "speaker": "host1",
      "text": "текст реплики",
      "image_index": 0
    }}
  ]
}}

image_index — индекс из списка изображений (0-based), или -1 если нет подходящего.
Равномерно распредели изображения по сегментам.

МАТЕРИАЛ СТАТЬИ:
{content}

ДОСТУПНЫЕ ИЗОБРАЖЕНИЯ:
{images}
""",

    "en": """You are a podcast scriptwriter in the style of Google NotebookLM.
Write a lively dialogue between two hosts (Host1 and Host2) based on the article.

RULES:
- Length: STRICTLY 3000–3500 words. That is ~7 minutes at natural English speaking pace. DO NOT EXCEED. Count your words.
- Tone: smart, curious, like Lex Fridman or Huberman Lab — not corporate, not stiff
- Host1 explains and digs into details, Host2 asks sharp questions and adds context
- Use specific facts, numbers, examples from the article — no vague generalisations
- Add natural transitions: "wait, so that means...", "hold on", "right, and the thing is"
- Do NOT say "as stated in the article", "the authors claim" — speak as an expert
- Host2 NEVER uses hollow exclamations like "That's incredible!", "Wow!", "Fascinating!" — reacts thoughtfully
- Each Host2 reply either asks a specific question OR adds a related fact or analogy
- Mix reply lengths — some short (1-2 sentences), some longer and more developed
- Structure: problem → method → results → implications → takeaway
- MANDATORY: last 2 segments must be a sign-off. Host1 wraps up (2-3 sentences), Host2 says goodbye to the audience ("Thanks for listening, see you next time!")

RESPONSE FORMAT — strict JSON, no markdown:
{{
  "title": "topic title (up to 60 characters)",
  "key_points": ["point 1", "point 2", "point 3"],
  "segments": [
    {{
      "id": 1,
      "speaker": "host1",
      "text": "replica text",
      "image_index": 0
    }}
  ]
}}

image_index — index from the images list (0-based), or -1 if none fits.
Distribute images evenly across segments.

ARTICLE CONTENT:
{content}

AVAILABLE IMAGES:
{images}
""",
}


def _build_sampled_text(blocks: list[dict], max_chars: int = 15000) -> str:
    """
    Для больших документов (много страниц) берёт текст равномерно
    по всему документу, а не только начало.
    Для маленьких — берёт весь текст как раньше.
    """
    full_text = "\n\n".join(
        f"[Страница {b['page'] + 1}]\n{b['text']}" for b in blocks
    )

    if len(full_text) <= max_chars:
        return full_text

    n_pages = len(blocks)

    # Маленькие/средние документы (до ~40 стр) — берём начало как раньше
    if n_pages <= 40:
        return full_text[:max_chars] + "\n... [текст сокращён]"

    # Большие документы — сэмплируем равномерно: введение + раскиданные
    # по всему тексту куски + заключение
    log.info(f"Большой документ ({n_pages} стр.) — сэмплирую текст равномерно")

    intro_pages   = blocks[:3]                       # первые 3 страницы — введение
    outro_pages   = blocks[-2:]                       # последние 2 — выводы
    middle_pages  = blocks[3:-2]

    budget_intro  = int(max_chars * 0.25)
    budget_outro  = int(max_chars * 0.15)
    budget_middle = max_chars - budget_intro - budget_outro

    def render(pages):
        return "\n\n".join(f"[Страница {b['page']+1}]\n{b['text']}" for b in pages)

    intro_text = render(intro_pages)[:budget_intro]
    outro_text = render(outro_pages)[:budget_outro]

    # Равномерно берём кусочки текста из середины документа
    if middle_pages:
        n_samples = min(12, len(middle_pages))         # до 12 точек по документу
        step = max(1, len(middle_pages) // n_samples)
        chars_per_sample = budget_middle // n_samples

        middle_chunks = []
        for i in range(0, len(middle_pages), step):
            page = middle_pages[i]
            chunk = page["text"][:chars_per_sample]
            if chunk.strip():
                middle_chunks.append(f"[Страница {page['page']+1}]\n{chunk}")
        middle_text = "\n\n".join(middle_chunks)
    else:
        middle_text = ""

    result = (
        f"{intro_text}\n\n"
        f"... [пропущены страницы, документ большой — показаны ключевые отрывки] ...\n\n"
        f"{middle_text}\n\n"
        f"... [конец основной части] ...\n\n"
        f"{outro_text}"
    )
    log.info(f"Сэмплировано {len(result)} символов из {n_pages} страниц")
    return result


async def generate_script(blocks: list[dict], job_dir: Path, lang: str = "ru") -> dict:
    """
    Генерирует скрипт через Gemini 2.5 Flash.
    Возвращает dict со структурой скрипта.
    Сохраняет в job_dir/script.json
    """
    # Собрать текст всех страниц
    full_text = _build_sampled_text(blocks, max_chars=15000)

    # Список изображений для LLM
    all_images = []
    for b in blocks:
        for img in b["images"]:
            all_images.append(
                f"{len(all_images)}: стр.{b['page']+1} — {img['caption'] or 'изображение'}"
            )
    images_str = "\n".join(all_images) if all_images else "нет изображений"

    # Полный промпт для Gemini (15K символов контекст)
    prompt_full = PROMPTS[lang].format(content=full_text, images=images_str)

    # Короткий промпт для Groq (обрезаем текст до 6K — лимит payload)
    short_text = (full_text[:6000] + "\n... [текст сокращён]") if len(full_text) > 6000 else full_text
    prompt_short = PROMPTS[lang].format(content=short_text, images=images_str)

    # Сначала пробуем Gemini, при ошибке — Groq
    response_text = await _call_gemini_then_groq(prompt_full, prompt_short, lang)

    script = _parse_json(response_text)

    # Жёсткая обрезка по языку:
    # EN: ~150 слов/мин → 7 мин = 1050 слов (с паузами ~3500 слов в тексте)
    # RU: ~120 слов/мин → 7 мин = 840 слов (с паузами ~3800 слов в тексте)
    max_w = 3500 if lang == "en" else 3800
    script = _trim_script(script, max_words=max_w)

    # Привязать реальные пути изображений к сегментам
    flat_images = [img for b in blocks for img in b["images"]]
    for seg in script.get("segments", []):
        idx = seg.get("image_index", -1)
        if 0 <= idx < len(flat_images):
            seg["image_path"] = flat_images[idx]["path"]
            seg["image_caption"] = flat_images[idx]["caption"]
        else:
            seg["image_path"] = None
            seg["image_caption"] = ""

    # Сохранить
    out_path = job_dir / "script.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(script, f, ensure_ascii=False, indent=2)

    log.info(f"Скрипт готов: {len(script.get('segments', []))} сегментов → {out_path}")
    return script


# Каждая модель Gemini имеет свою отдельную квоту (free tier ~20 req/min на модель),
# поэтому при исчерпании лимита у одной модели выгоднее сразу пробовать следующую,
# а не ждать сброса лимита той же модели. Порядок: новее/качественнее → проще.
GEMINI_MODEL_CANDIDATES = [
    "gemini-2.5-flash",
    "gemini-3-flash-preview",
    "gemini-3.5-flash",
    "gemini-3.6-flash",
    "gemini-3.7-flash",
    "gemini-2.5-flash-lite",
    "gemini-3.1-flash-lite",
    "gemini-2.5-pro",
]


def _is_rate_limited(err: str, type_name: str) -> bool:
    # google.api_core.exceptions.ResourceExhausted (квота) не содержит "429" в тексте,
    # поэтому проверяем и по типу исключения, и по ключевым словам в сообщении.
    return (
        "429" in err or "resourceexhausted" in type_name.lower()
        or "quota" in err.lower() or "rate limit" in err.lower()
    )


async def _call_gemini_then_groq(prompt_full: str, prompt_short: str, lang: str) -> str:
    """Перебирает модели Gemini по очереди (у каждой свой лимит квоты).
    Если ни одна не сработала — Groq с коротким промптом."""
    last_err = None

    for model_name in GEMINI_MODEL_CANDIDATES:
        try:
            model = genai.GenerativeModel(model_name)
            response = await asyncio.to_thread(model.generate_content, prompt_full)
            log.info(f"Gemini {model_name}: успешно")
            return response.text
        except Exception as e:
            last_err = e
            err = str(e)
            if _is_rate_limited(err, type(e).__name__):
                log.warning(f"Gemini {model_name}: квота исчерпана, пробую следующую модель...")
            else:
                log.warning(f"Gemini {model_name}: {err[:120]}, пробую следующую модель...")

    log.warning(f"Все модели Gemini недоступны (последняя ошибка: {str(last_err)[:120]}), переключаюсь на Groq...")

    if not GROQ_API_KEY:
        raise RuntimeError(
            "Все модели Gemini недоступны, а GROQ_API_KEY не задан в .env. "
            "Получи ключ на https://console.groq.com и добавь в .env"
        )
    log.info(f"Groq промпт: {len(prompt_short)} символов")
    return await _call_groq(prompt_short, lang)


async def _call_groq(prompt: str, lang: str) -> str:
    """Вызов Groq через OpenAI-совместимый API."""
    import httpx

    system = (
        "Ты — профессиональный сценарист подкастов. Отвечай строго в JSON."
        if lang == "ru"
        else "You are a professional podcast scriptwriter. Reply strictly in JSON."
    )

    headers = {
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 16000,  # GROQ_MODEL is a reasoning model (gpt-oss) — reasoning tokens
                              # share this budget with the actual JSON output, so give it room.
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
            if resp.status_code == 429:
                wait = 30 * (attempt + 1)
                log.warning(f"Groq 429, жду {wait}с (попытка {attempt+1})")
                await asyncio.sleep(wait)
                continue
            if resp.status_code == 413:
                log.warning(f"Groq 413: промпт слишком большой ({len(prompt)} симв), обрезаю до 4000")
                if len(prompt) > 4000:
                    prompt = prompt[:4000] + "\n... [сокращено]\nОтветь строго в JSON."
                    payload["messages"][1]["content"] = prompt
                continue
            if resp.status_code == 400:
                # Показать точную причину от Groq вместо общей ошибки
                try:
                    err_body = resp.json()
                    err_msg = err_body.get("error", {}).get("message", resp.text[:300])
                except Exception:
                    err_msg = resp.text[:300]
                log.error(f"Groq 400 Bad Request: {err_msg}")
                raise RuntimeError(f"Groq 400: {err_msg}")
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            log.info(f"Groq ответил успешно ({len(text)} символов)")
            return text
        except Exception as e:
            if attempt == 2:
                raise RuntimeError(f"Groq тоже упал: {e}")
            await asyncio.sleep(10)

    raise RuntimeError("Groq: превышено количество попыток")


def _trim_script(script: dict, max_words: int = 5000) -> dict:
    """
    Обрезает скрипт до max_words слов.
    Удаляет сегменты с конца, но сохраняет последние 2 (прощание).
    """
    segments = script.get("segments", [])
    if not segments:
        return script

    # Считаем слова
    total = sum(len(s.get("text","").split()) for s in segments)
    if total <= max_words:
        log.info(f"Скрипт {total} слов — в пределах лимита")
        return script

    log.warning(f"Скрипт {total} слов > {max_words}, обрезаю...")

    # Последние 2 сегмента — прощание, не трогаем
    farewell = segments[-2:] if len(segments) >= 2 else segments[-1:]
    body     = segments[:-len(farewell)]

    kept = []
    words = 0
    budget = max_words - sum(len(s.get("text","").split()) for s in farewell)

    for seg in body:
        w = len(seg.get("text","").split())
        if words + w > budget:
            break
        kept.append(seg)
        words += w

    script["segments"] = kept + farewell
    final = sum(len(s.get("text","").split()) for s in script["segments"])
    log.info(f"После обрезки: {final} слов, {len(script['segments'])} сегментов")
    return script


def _parse_json(text: str) -> dict:
    """
    Извлечь JSON из ответа LLM.
    Обрабатывает: ```json блоки, лишний текст после JSON, два объекта подряд.
    """
    text = text.strip()

    # 1. Убрать markdown ```json ... ``` обёртку
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```\s*$", "", text)
    text = text.strip()

    # 2. Попробовать напрямую
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # 3. Найти первый полный JSON-объект через decoder (игнорирует хвост)
    try:
        decoder = json.JSONDecoder()
        obj, _ = decoder.raw_decode(text)
        if isinstance(obj, dict):
            log.warning("JSON содержал лишние данные после объекта — обрезано")
            return obj
    except json.JSONDecodeError:
        pass

    # 4. Найти самый большой {...} блок в тексте
    start = text.find("{")
    if start != -1:
        # Ищем закрывающую скобку с учётом вложенности
        depth = 0
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:i+1]
                    try:
                        obj = json.loads(candidate)
                        log.warning("JSON извлечён из середины текста")
                        return obj
                    except json.JSONDecodeError:
                        break

    log.error(f"Не удалось распарсить JSON от LLM\nТекст: {text[:500]}")
    raise ValueError(f"LLM вернул невалидный JSON: {text[:200]}")