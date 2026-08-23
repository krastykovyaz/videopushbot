"""
Очередь задач на asyncio — без Redis, без Celery.
При MAX_WORKERS=1 задачи обрабатываются строго по очереди
(важно для TTS: не хватает RAM на параллельный Chatterbox).
"""

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Awaitable

log = logging.getLogger("queue_worker")


@dataclass
class Job:
    job_id:      str
    user_id:     int
    chat_id:     int
    pdf_path:    Path
    job_dir:     Path
    lang:        str                          # "ru" | "en"
    progress_fn: Callable[[int, str], Awaitable[None]]  # progress(chat_id, text)


class JobQueue:
    def __init__(self, max_workers: int = 1):
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._max_workers = max_workers
        self._active: dict[int, str] = {}    # user_id → job_id

    def queue_size(self) -> int:
        return self._queue.qsize()

    def position(self, user_id: int) -> int:
        """Позиция пользователя в очереди (0 = нет задачи)."""
        jobs = list(self._queue._queue)       # type: ignore
        for i, job in enumerate(jobs, 1):
            if job.user_id == user_id:
                return i
        return 0

    async def enqueue(self, job: Job):
        await self._queue.put(job)
        log.info(f"Job {job.job_id} добавлен (user {job.user_id}), размер очереди: {self._queue.qsize()}")

    async def run(self, on_done_callback):
        """Основной цикл воркера. Запускать как asyncio.create_task()."""
        semaphore = asyncio.Semaphore(self._max_workers)

        async def process(job: Job):
            async with semaphore:
                self._active[job.user_id] = job.job_id
                video_path = thumb_path = None
                error = None
                try:
                    video_path, thumb_path = await run_pipeline(job)
                except Exception as e:
                    log.exception(f"Ошибка в job {job.job_id}")
                    error = str(e)
                finally:
                    self._active.pop(job.user_id, None)
                    await on_done_callback(job, video_path, thumb_path, error)

        while True:
            job = await self._queue.get()
            asyncio.create_task(process(job))
            self._queue.task_done()


# ── Pipeline ─────────────────────────────────────────────────────────────────
async def run_pipeline(job: Job) -> tuple[Path, Path]:
    """
    Запускает все шаги пайплайна последовательно.
    Каждый шаг — отдельный модуль в pipeline/.
    Возвращает (video_path, thumbnail_path).
    """
    from pipeline.step01_extract  import extract_pdf
    from pipeline.step02_script   import generate_script
    from pipeline.step03_tts      import generate_tts
    from pipeline.step04_frames   import build_frames
    from pipeline.step05_video    import build_video
    from pipeline.step06_thumbnail import make_thumbnail

    p = job.progress_fn
    cid = job.chat_id
    d = job.job_dir

    # Шаг 1 — Извлечение
    await p(cid, "⏳ Шаг 1/6 — извлекаю текст и картинки из PDF...")
    blocks = await asyncio.to_thread(extract_pdf, job.pdf_path, d)
    n_pages = len(blocks)
    n_imgs  = sum(len(b.get("images", [])) for b in blocks)
    await p(cid, f"✅ Шаг 1/6 — извлечено: {n_pages} стр., {n_imgs} изображений")

    # Шаг 2 — Скрипт
    await p(cid, "⏳ Шаг 2/6 — генерирую скрипт диалога (LLM)...")
    script = await generate_script(blocks, d, lang=job.lang)
    word_count = sum(len(s["text"].split()) for s in script["segments"])
    est_min = word_count // 130
    warn = " ⚠️ длиннее ожидаемого" if word_count > 5500 else ""
    await p(cid, f"✅ Шаг 2/6 — скрипт готов: {word_count} слов (~{est_min} мин){warn}")

    # Шаг 3 — TTS
    await p(cid, "⏳ Шаг 3/6 — озвучиваю (Chatterbox TTS)... это займёт пару минут")
    timeline = await asyncio.to_thread(generate_tts, script, d, lang=job.lang)
    await p(cid, f"✅ Шаг 3/6 — аудио готово: {len(timeline)} сегментов")

    # Шаг 4 — Кадры
    await p(cid, "⏳ Шаг 4/6 — собираю кадры видео...")
    frames_dir = await asyncio.to_thread(build_frames, script, timeline, d, job.lang)
    await p(cid, f"✅ Шаг 4/6 — кадры готовы")

    # Шаг 5 — Видео
    await p(cid, "⏳ Шаг 5/6 — рендерю видео (ffmpeg)...")
    video_path = await asyncio.to_thread(build_video, frames_dir, timeline, d)
    size_mb = video_path.stat().st_size / 1024 / 1024
    from pydub import AudioSegment as _AS
    _audio = _AS.from_mp3(str(d / "final_audio.mp3"))
    real_sec = len(_audio) / 1000
    real_min = int(real_sec // 60)
    real_s   = int(real_sec % 60)
    time_warn = " ⚠️ короче 5 мин" if real_sec < 300 else (" ⚠️ длиннее 8 мин" if real_sec > 480 else "")
    await p(cid, f"✅ Шаг 5/6 — видео: {size_mb:.0f} МБ, длительность {real_min}:{real_s:02d}{time_warn}")

    # Шаг 6 — Thumbnail
    await p(cid, "⏳ Шаг 6/6 — делаю превью...")
    thumb_path = await asyncio.to_thread(make_thumbnail, script, blocks, d)
    await p(cid, "✅ Шаг 6/6 — превью готово!")

    return video_path, thumb_path