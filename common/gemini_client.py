"""
Shared Gemini content generator — merges the RU/EN GeminiGenerator classes that
used to be duplicated in telegram_file_reciever_ru2.py / telegram_file_reciever2.py,
and adds topic classification for automatic channel routing.
"""

import logging
import re

import google.generativeai as genai

_DESCRIPTION_PROMPTS = {
    "ru": """
Вы — эксперт в создании описаний для видео на YouTube.

НАЗВАНИЕ ДОКУМЕНТА: {title}
СОДЕРЖАНИЕ ДОКУМЕНТА: {content}

Создайте короткое, привлекательное описание для YouTube:
- 3-5 предложений (максимум 300 символов)
- Начните с убедительного вопроса или факта
- Простой язык, 2-3 эмодзи
- Без академического языка
- Верните ТОЛЬКО текст описания

Описание:
""",
    "en": """
You're an expert at creating YouTube video descriptions.

DOCUMENT TITLE: {title}
DOCUMENT CONTENTS: {content}

Create a short, engaging YouTube description:
- 3-5 sentences (max 300 characters)
- Start with a compelling question or fact
- Simple language, 2-3 emojis
- NO academic language
- Return ONLY the description text

Description:
""",
}

_FALLBACK_DESCRIPTION = {
    "ru": "Обзор: {title}",
    "en": "Overview: {title}",
}

_RU_FOOTER = (
    "\n\nПоддержка: https://boosty.to/krastykovyaz\n"
    "Подписывайся - https://t.me/arxivpaper\n"
    "создано с помощью NotebookLM"
)

_EN_FOOTER = (
    "\n\nDonats: https://www.patreon.com/c/luxak\n\n"
    "subscribe - https://t.me/arxivpaper\n"
    "created with NotebookLM"
)

_CLASSIFY_PROMPT = """
You are a content classifier for a YouTube/VK channel network.

DOCUMENT TITLE: {title}
DOCUMENT CONTENT SAMPLE: {content}

Pick exactly ONE category from this list that best fits the document:
{categories}

Return ONLY the category name, exactly as written above, with no extra text.
"""

# Each Gemini model has its own separate free-tier quota (~20 req/min), so when one
# is exhausted it's faster to try the next model than to wait out its rate limit.
_FALLBACK_MODELS = [
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
    # google.api_core.exceptions.ResourceExhausted (quota) doesn't contain "429" in
    # its text, so check the exception type name too.
    return ("429" in err or "resourceexhausted" in type_name.lower()
            or "quota" in err.lower() or "rate limit" in err.lower())


def append_footer(description: str, lang: str) -> str:
    """Shared by Gemini-generated descriptions and channel-sourced ones (see
    bot.py's arxiv channel listeners), so the support/subscribe footer is
    consistent regardless of where the description text came from."""
    if lang == "ru":
        return description + _RU_FOOTER
    elif lang == "en":
        return description + _EN_FOOTER
    return description


class GeminiContentGenerator:
    # Without a timeout a stalled request can hang indefinitely; since these
    # calls run inside the pipeline's single worker, that freezes the whole
    # job queue until the process is restarted.
    REQUEST_TIMEOUT_SECONDS = 60

    def __init__(self, api_key, model_name):
        genai.configure(api_key=api_key)
        # Try the configured model first, then rotate through the rest on quota errors.
        self.model_candidates = [model_name] + [m for m in _FALLBACK_MODELS if m != model_name]
        logging.info("✅ Gemini API initialized")

    def _generate(self, prompt: str) -> str:
        last_err = None
        for model_name in self.model_candidates:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    prompt, request_options={"timeout": self.REQUEST_TIMEOUT_SECONDS})
                return response.text.strip()
            except Exception as e:
                last_err = e
                reason = "quota exceeded" if _is_rate_limited(str(e), type(e).__name__) else str(e)[:100]
                logging.warning(f"Gemini {model_name}: {reason}, trying next model...")
        raise last_err

    def generate_description(self, title, content, lang="ru"):
        if len(content) > 5000:
            content = content[:5000] + "..."

        prompt = _DESCRIPTION_PROMPTS.get(lang, _DESCRIPTION_PROMPTS["en"]).format(
            title=title, content=content)

        try:
            description = self._generate(prompt)
        except Exception as e:
            logging.error(f"❌ Gemini error (all models failed): {e}")
            description = _FALLBACK_DESCRIPTION.get(lang, _FALLBACK_DESCRIPTION["en"]).format(title=title)

        return append_footer(description, lang)

    def classify_topic(self, title, content, categories):
        if len(content) > 3000:
            content = content[:3000] + "..."

        prompt = _CLASSIFY_PROMPT.format(
            title=title, content=content, categories="\n".join(f"- {c}" for c in categories))

        try:
            answer = self._generate(prompt)
        except Exception as e:
            logging.error(f"❌ Gemini classify error (all models failed): {e}")
            return None

        for category in categories:
            if category.lower() in answer.lower():
                return category
        logging.warning(f"⚠️ classify_topic: unrecognized answer '{answer}', no category matched")
        return None

    def pick_most_interesting(self, candidates: list[dict]) -> int:
        """candidates: [{"title": str, "snippet": str}, ...]. Returns the
        index of the single most interesting/newsworthy one, or 0 as a safe
        fallback if Gemini's answer can't be parsed."""
        listing = "\n\n".join(
            f"[{i}] {c['title']}\n{c.get('snippet', '')[:300]}"
            for i, c in enumerate(candidates)
        )
        prompt = (
            "Ниже — список свежих новостей. Выбери ОДНУ самую интересную и "
            "значимую для широкой аудитории (не узкоспециальную, с потенциалом "
            "вызвать эмоции или дискуссию). Ответь ТОЛЬКО номером в квадратных "
            f"скобках, без пояснений.\n\n{listing}"
        )
        try:
            answer = self._generate(prompt)
        except Exception as e:
            logging.error(f"❌ Gemini pick_most_interesting error (all models failed): {e}")
            return 0

        match = re.search(r"\d+", answer)
        if match:
            idx = int(match.group())
            if 0 <= idx < len(candidates):
                return idx
        logging.warning(f"⚠️ pick_most_interesting: unparseable answer '{answer}', defaulting to 0")
        return 0
