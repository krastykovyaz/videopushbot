"""
Шаг 1: Извлечение текста и изображений из PDF.
Привязывает каждое изображение к ближайшему текстовому блоку.
"""

import json
import logging
from pathlib import Path

import fitz  # pymupdf

log = logging.getLogger("step01")

FIGURE_RENDER_WIDTH = 2800   # px: wide enough that a figure zoomed 1.5x still shows real detail on 1080p
PAGE_RENDER_WIDTH = 1900
MAX_FIGURES = 14             # vector figures kept per document (captioned ones first)
MIN_VISUALS = 3              # below this many figures, text pages are added as scrolled pages
_CAPTION_KEYWORDS = ("figure", "fig.", "fig ", "table", "рис.", "рисунок", "таблица")


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
    page_candidates = []     # pages with vector graphics but no figure found

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
        raster_boxes = []
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
                if img_bbox:
                    raster_boxes.append(fitz.Rect(img_bbox))

                # Подпись: ищем строку "Figure N:" / "Рис. N:" рядом
                caption = _find_caption(text_blocks, bbox_y, page.rect.height)

                images.append({
                    "path": str(img_path),
                    "caption": caption,
                    "bbox_y": bbox_y,
                    "kind": "figure",
                })
            except Exception as e:
                log.warning(f"Не удалось извлечь изображение p{page_num} i{img_idx}: {e}")

        # ── Векторные рисунки (графики, схемы, таблицы) ──────────────────────
        # Whole pages made figures unreadable (a diagram was ~15% of the frame),
        # so the figure itself is cropped out and rendered large.
        regions = _figure_regions(page, text_blocks, raster_boxes)
        for reg_idx, rect in enumerate(regions):
            try:
                scale = max(3.0, min(8.0, FIGURE_RENDER_WIDTH / max(rect.width, 1)))
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect)
                img_path = img_dir / f"p{page_num:03d}_v{reg_idx:02d}.png"
                pix.save(str(img_path))
                images.append({
                    "path": str(img_path),
                    "caption": _find_figure_caption(text_blocks, rect, page.rect.height),
                    "bbox_y": rect.y0,
                    "kind": "figure",
                    "area": rect.get_area(),
                })
            except Exception as e:
                log.warning(f"Не удалось отрисовать рисунок p{page_num} v{reg_idx}: {e}")

        if not images and _has_vector_graphics(page):
            page_candidates.append(page_num)

        pages.append({
            "page": page_num,
            "text": full_text,
            "images": images,
        })

    _limit_figures(pages)

    # Pages without a recognisable figure are shown whole (and scrolled by the
    # video renderer) only when the paper would otherwise have almost no visuals.
    have = sum(len(p["images"]) for p in pages)
    for page_num in page_candidates[:max(0, MIN_VISUALS - have)]:
        try:
            page = doc[page_num]
            rect = _content_rect(page)
            scale = max(2.0, min(4.0, PAGE_RENDER_WIDTH / max(rect.width, 1)))
            img_path = img_dir / f"p{page_num:03d}_render.png"
            page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect).save(str(img_path))
            pages[page_num]["images"].append({
                "path": str(img_path), "caption": f"Страница {page_num + 1}",
                "bbox_y": 0.0, "kind": "page",
            })
        except Exception as e:
            log.warning(f"Не удалось отрисовать страницу {page_num + 1}: {e}")

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


def _merge_rects(rects: list, gap: float) -> list:
    """Unions rectangles that overlap or lie within `gap` points of each other."""
    rects = [fitz.Rect(r) for r in rects]
    merged = True
    while merged:
        merged = False
        for i in range(len(rects)):
            for j in range(i + 1, len(rects)):
                grown = fitz.Rect(rects[i].x0 - gap, rects[i].y0 - gap, rects[i].x1 + gap, rects[i].y1 + gap)
                if grown.intersects(rects[j]):
                    rects[i] |= rects[j]
                    del rects[j]
                    merged = True
                    break
            if merged:
                break
    return rects


def _is_caption(text: str) -> bool:
    return text.strip().lower().startswith(_CAPTION_KEYWORDS)


def _figure_regions(page, text_blocks: list[dict], raster_boxes: list) -> list:
    """Figure-sized clusters of vector drawings, grown to include their labels
    (axis text, legends, text inside boxes) and trimmed of page-wide frames."""
    pr = page.rect
    try:
        clusters = page.cluster_drawings()
    except Exception:
        return []
    cands = [r for r in clusters
             if r.width >= pr.width * 0.18 and r.height >= pr.height * 0.07
             and pr.get_area() * 0.03 <= r.get_area() <= pr.get_area() * 0.9]
    if not cands:
        return []
    regions = []
    for rect in _merge_rects(cands, gap=14):
        probe = fitz.Rect(rect.x0 - 8, rect.y0 - 8, rect.x1 + 8, rect.y1 + 8)
        for b in page.get_text("blocks"):
            if b[6] != 0 or not b[4].strip():
                continue
            brect = fitz.Rect(b[:4])
            if brect.intersects(probe) and not _is_caption(b[4]) and brect.height < 90:
                rect |= brect
        rect = fitz.Rect(rect.x0 - 6, rect.y0 - 6, rect.x1 + 6, rect.y1 + 6) & pr
        if rect.is_empty or rect.width < pr.width * 0.2:
            continue
        # a raster picture already extracted separately: don't show it twice
        if any(rb.get_area() > 0 and (rb & rect).get_area() / rb.get_area() > 0.6 and rb.get_area() > rect.get_area() * 0.6
               for rb in raster_boxes):
            continue
        regions.append(rect)
    return regions[:3]


def _find_figure_caption(text_blocks: list[dict], rect, page_height: float) -> str:
    """Caption below the figure (figures) or above it (tables)."""
    best, best_gap = "", page_height * 0.12
    for block in text_blocks:
        lines = block["text"].split("\n")
        first = lines[0]
        if not _is_caption(first):
            continue
        if len(first) < 14 and len(lines) > 1:     # "TABLE I" + its title on the next line
            first = f"{first} {lines[1]}"
        gap = block["y0"] - rect.y1 if block["y0"] >= rect.y1 - 4 else rect.y0 - block["y1"]
        if -4 <= gap < best_gap:
            best, best_gap = first[:120], gap
    return best


def _content_rect(page):
    """Bounding box of everything on the page (text + drawings), margins trimmed."""
    rects = [fitz.Rect(b[:4]) for b in page.get_text("blocks") if b[4].strip()]
    rects += [fitz.Rect(d["rect"]) for d in page.get_drawings() if d.get("rect")]
    if not rects:
        return page.rect
    box = rects[0]
    for r in rects[1:]:
        box |= r
    return fitz.Rect(box.x0 - 10, box.y0 - 10, box.x1 + 10, box.y1 + 10) & page.rect


def _limit_figures(pages: list[dict]):
    """Keeps the best MAX_FIGURES vector figures of the document: captioned first,
    then the biggest. Raster figures are never dropped."""
    vec = [(pn, i) for pn, p in enumerate(pages) for i, im in enumerate(p["images"]) if "area" in im]
    if len(vec) <= MAX_FIGURES:
        for p in pages:
            for im in p["images"]:
                im.pop("area", None)
        return
    ranked = sorted(vec, key=lambda k: (not pages[k[0]]["images"][k[1]]["caption"],
                                        -pages[k[0]]["images"][k[1]]["area"]))
    drop = set(ranked[MAX_FIGURES:])
    for pn, p in enumerate(pages):
        kept = []
        for i, im in enumerate(p["images"]):
            if (pn, i) in drop:
                Path(im["path"]).unlink(missing_ok=True)
                continue
            im.pop("area", None)
            kept.append(im)
        p["images"] = kept
