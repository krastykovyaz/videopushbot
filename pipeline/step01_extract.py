"""
Шаг 1: Извлечение текста и изображений из PDF.
Привязывает каждое изображение к ближайшему текстовому блоку.
"""

import json
import logging
from pathlib import Path

import fitz  # pymupdf

log = logging.getLogger("step01")


def extract_pdf(pdf_path: Path, job_dir: Path) -> list[dict]:
    """
    Возвращает список блоков:
    {
      "page": int,
      "text": str,
      "images": [{"path": str, "caption": str, "bbox_y": float}]
    }
    Сохраняет результат в job_dir/extracted/text_blocks.json
    """
    out_dir = job_dir / "extracted"
    img_dir = out_dir / "images"
    out_dir.mkdir(parents=True, exist_ok=True)
    img_dir.mkdir(parents=True, exist_ok=True)

    doc = fitz.open(str(pdf_path))
    pages = []

    for page_num in range(len(doc)):
        page = doc[page_num]

        # ── Текстовые блоки с координатами ──────────────────────────────────
        raw_blocks = page.get_text("blocks")
        # raw_blocks: (x0, y0, x1, y1, text, block_no, block_type)
        text_blocks = [
            {"text": b[4].strip(), "y0": b[1], "y1": b[3]}
            for b in raw_blocks
            if b[6] == 0 and b[4].strip()  # type 0 = text
        ]
        full_text = "\n".join(b["text"] for b in text_blocks)

        # ── Изображения ──────────────────────────────────────────────────────
        images = []
        for img_idx, img_info in enumerate(page.get_images(full=True)):
            xref = img_info[0]
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n > 4:  # CMYK → RGB
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                if pix.width < 100 or pix.height < 100:  # пропустить иконки
                    continue

                # Фильтр мусорных картинок:
                # 1. Слишком узкие/широкие (палитры, разделители, UI-элементы)
                aspect = pix.width / pix.height
                if aspect > 5.0 or aspect < 0.2:
                    log.info(f"Пропускаю картинку {pix.width}x{pix.height} (aspect {aspect:.1f})")
                    continue
                # 2. Слишком маленькая площадь (декоративные элементы)
                if pix.width * pix.height < 40000:  # меньше ~200x200
                    continue

                img_filename = f"p{page_num:03d}_i{img_idx:02d}.png"
                img_path = img_dir / img_filename
                pix.save(str(img_path))

                # bbox изображения на странице
                img_bbox = page.get_image_bbox(img_info)
                bbox_y = img_bbox.y0 if img_bbox else 0.0

                # Подпись: ищем строку "Figure N:" / "Рис. N:" рядом
                caption = _find_caption(text_blocks, bbox_y, page.rect.height)

                images.append({
                    "path": str(img_path),
                    "caption": caption,
                    "bbox_y": bbox_y,
                })
            except Exception as e:
                log.warning(f"Не удалось извлечь изображение p{page_num} i{img_idx}: {e}")

        # ── Фоллбэк: рендер области страницы если нет растровых картинок ────
        # Для векторной графики (графики, диаграммы) рендерим всю страницу
        if not images and _has_vector_graphics(page):
            img_filename = f"p{page_num:03d}_render.png"
            img_path = img_dir / img_filename
            mat = fitz.Matrix(1.5, 1.5)
            pix = page.get_pixmap(matrix=mat)
            pix.save(str(img_path))
            images.append({
                "path": str(img_path),
                "caption": f"Страница {page_num + 1}",
                "bbox_y": 0.0,
            })

        pages.append({
            "page": page_num,
            "text": full_text,
            "images": images,
        })

    doc.close()

    # Сохранить JSON
    out_json = out_dir / "text_blocks.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)

    log.info(f"Извлечено {len(pages)} страниц, "
             f"{sum(len(p['images']) for p in pages)} изображений → {out_json}")
    return pages


def _find_caption(text_blocks: list[dict], img_y: float, page_height: float) -> str:
    """Найти подпись Figure/Рис. рядом с изображением."""
    keywords = ("figure", "fig.", "рис.", "рисунок", "chart", "graph", "table", "таблица")
    # Ищем в блоках ниже картинки (в пределах 20% высоты страницы)
    for block in text_blocks:
        if block["y0"] >= img_y and (block["y0"] - img_y) < page_height * 0.2:
            first_line = block["text"].split("\n")[0].lower()
            if any(kw in first_line for kw in keywords):
                return block["text"].split("\n")[0][:120]
    return ""


def _has_vector_graphics(page) -> bool:
    """Проверить, есть ли на странице векторные пути (графики, диаграммы)."""
    paths = page.get_drawings()
    return len(paths) > 10  # больше 10 путей — скорее всего диаграмма