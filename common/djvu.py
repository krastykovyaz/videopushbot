"""DjVu -> PDF, so scanned books/papers sent as .djvu go through the normal PDF pipeline.

Uses djvulibre's `ddjvu` (apt: djvulibre-bin). A DjVu without a text layer comes
out as page images only, which has nothing to read aloud — that case is reported
to the user instead of failing later in the pipeline.
"""

import shutil
import subprocess
from pathlib import Path

CONVERT_TIMEOUT = 900


class DjvuError(Exception):
    pass


def djvu_to_pdf(src: Path, dst: Path) -> Path:
    exe = shutil.which("ddjvu")
    if not exe:
        raise DjvuError("На сервере не установлен ddjvu (пакет djvulibre-bin) — DjVu пока не поддерживается.")
    try:
        proc = subprocess.run([exe, "-format=pdf", "-quality=85", str(src), str(dst)],
                              capture_output=True, text=True, timeout=CONVERT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DjvuError("Конвертация DjVu заняла больше 15 минут — файл слишком большой.") from None
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        dst.unlink(missing_ok=True)
        raise DjvuError(f"Не удалось сконвертировать DjVu: {(proc.stderr or '').strip()[:150] or 'ошибка ddjvu'}")
    if not _has_text(dst):
        dst.unlink(missing_ok=True)
        raise DjvuError("В этом DjVu только картинки страниц, без текстового слоя — читать вслух нечего (нужен OCR).")
    return dst


def _has_text(pdf: Path) -> bool:
    import fitz
    with fitz.open(str(pdf)) as doc:
        return sum(len(page.get_text().strip()) for page in list(doc)[:10]) > 200
