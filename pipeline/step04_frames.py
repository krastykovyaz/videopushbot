"""
Шаг 4: Сборка кадров видео 1920x1080.
Каждый кадр = картинка из PDF + субтитры + иконка ведущего.
"""

import json
import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageEnhance

log = logging.getLogger("step04")

# Размер кадра
W, H = 1920, 1080

# Цвета
BG_COLOR        = (15, 15, 20)
OVERLAY_COLOR   = (0, 0, 0, 160)
TEXT_COLOR      = (255, 255, 255)
SUBTITLE_BG     = (0, 0, 0, 180)
HOST1_COLOR     = (64, 196, 255)   # синий
HOST2_COLOR     = (255, 160, 64)   # оранжевый
CAPTION_COLOR   = (200, 200, 200)

SPEAKER_LABELS = {
    "ru": {"host1": "Ведущий 1", "host2": "Ведущий 2"},
    "en": {"host1": "Host 1",    "host2": "Host 2"},
}


def build_frames(script: dict, timeline: list[dict], job_dir: Path, lang: str = "ru") -> Path:
    """
    Создаёт PNG-кадр для каждого сегмента.
    Возвращает путь к папке frames/.
    """
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    fonts = _load_fonts()
    title = script.get("title", "")

    for entry in timeline:
        frame_path = frames_dir / f"frame_{entry['seg_id']:04d}.png"
        if frame_path.exists():
            continue  # кэш

        img = _make_frame(
            image_path=entry.get("image_path"),
            speaker=entry["speaker"],
            text=entry["text"],
            caption=entry.get("image_caption", ""),
            title=title,
            fonts=fonts,
            lang=lang,
        )
        img.save(str(frame_path), "PNG", optimize=True)
        log.info(f"Кадр {entry['seg_id']+1}/{len(timeline)} сохранён")

    log.info(f"Все кадры → {frames_dir}")
    return frames_dir


def _make_frame(image_path, speaker, text, caption, title, fonts, lang) -> Image.Image:
    """
    Сетка кадра 1920x1080:
    ┌─────────────────────────────────────────────────────────────┐
    │  ЗАГОЛОВОК (вся ширина, 80px)                               │
    ├──────────────────────────────┬──────────────────────────────┤
    │  ЛЕВЫЙ БЛОК (текст)  ~55%    │  ПРАВЫЙ БЛОК (картинка) ~42% │
    │  - аватар + имя ведущего     │  - изображение из PDF         │
    │  - текст реплики             │    вписано с сохранением      │
    │                              │    пропорций, с рамкой        │
    │                              │  - подпись снизу              │
    └──────────────────────────────┴──────────────────────────────┘
    """
    canvas = Image.new("RGB", (W, H), BG_COLOR)
    draw   = ImageDraw.Draw(canvas)

    # ── Зоны ──────────────────────────────────────────────────────────────
    HEADER_H  = 80
    PAD       = 36                        # отступ от краёв
    DIVIDER_X = int(W * 0.56)            # граница левого/правого блока

    # Левый блок: текст
    LEFT_X1, LEFT_X2 = PAD, DIVIDER_X - PAD
    LEFT_Y1, LEFT_Y2 = HEADER_H + PAD, H - PAD

    # Правый блок: картинка
    RIGHT_X1, RIGHT_X2 = DIVIDER_X + PAD, W - PAD
    RIGHT_Y1, RIGHT_Y2 = HEADER_H + PAD, H - PAD

    # ── Тонкие вертикальные акценты слева ─────────────────────────────────
    speaker_color = HOST1_COLOR if speaker == "host1" else HOST2_COLOR
    draw.rectangle([0, 0, 4, H], fill=speaker_color)

    # ── Заголовок ─────────────────────────────────────────────────────────
    if title:
        # Полупрозрачная полоса под заголовком
        draw.rectangle([0, 0, W, HEADER_H], fill=(22, 22, 30))
        draw.text(
            (W // 2, HEADER_H // 2), title[:80],
            font=fonts["title"], fill=(220, 220, 230), anchor="mm"
        )
        # Тонкая линия-разделитель
        draw.rectangle([0, HEADER_H, W, HEADER_H + 1], fill=(50, 50, 70))

    # ── Правый блок: картинка из PDF ─────────────────────────────────────
    has_image = image_path and Path(str(image_path)).exists()
    if has_image:
        try:
            img = Image.open(str(image_path)).convert("RGB")
            iw, ih = img.size

            zone_w = RIGHT_X2 - RIGHT_X1
            zone_h = RIGHT_Y2 - RIGHT_Y1 - (50 if caption else 0)

            # Вписать с сохранением пропорций (fit, не crop)
            scale   = min(zone_w / iw, zone_h / ih)
            new_w   = int(iw * scale)
            new_h   = int(ih * scale)
            img     = img.resize((new_w, new_h), Image.LANCZOS)

            # Центрировать в правой зоне
            paste_x = RIGHT_X1 + (zone_w - new_w) // 2
            paste_y = RIGHT_Y1 + (zone_h - new_h) // 2

            # Тонкая рамка вокруг картинки
            border = 3
            draw.rectangle(
                [paste_x - border, paste_y - border,
                 paste_x + new_w + border, paste_y + new_h + border],
                fill=(60, 60, 80)
            )
            canvas.paste(img, (paste_x, paste_y))
            log.info(f"Картинка {iw}x{ih} → {new_w}x{new_h} на ({paste_x},{paste_y})")

            # Подпись под картинкой
            if caption:
                cap_y = RIGHT_Y2 - 36
                draw.text(
                    ((RIGHT_X1 + RIGHT_X2) // 2, cap_y),
                    caption[:70],
                    font=fonts["caption"], fill=CAPTION_COLOR, anchor="mm"
                )
        except Exception as e:
            log.warning(f"Не удалось загрузить изображение {image_path}: {e}")
            has_image = False

    # ── Левый блок или полный экран (если нет картинки) ─────────────────
    # Когда картинки нет — текст занимает весь экран (шире и крупнее)
    if has_image:
        text_x      = LEFT_X1
        text_right  = LEFT_X2
        divider_visible = True
    else:
        # Нет картинки — растягиваем текстовую зону на весь экран
        text_x      = PAD
        text_right  = W - PAD
        divider_visible = False

    if divider_visible:
        draw.rectangle([DIVIDER_X, HEADER_H + 1, DIVIDER_X + 1, H], fill=(40, 40, 60))

    label = SPEAKER_LABELS.get(lang, SPEAKER_LABELS["en"]).get(speaker, speaker)

    # Аватар-кружок
    r  = 32
    ax = text_x + r + 4
    ay = LEFT_Y1 + r + 10
    draw.ellipse([ax-r, ay-r, ax+r, ay+r], fill=speaker_color)
    draw.text(
        (ax, ay),
        "1" if speaker == "host1" else "2",
        font=fonts["avatar"], fill=(15, 15, 20), anchor="mm"
    )
    draw.text(
        (ax + r + 14, ay), label,
        font=fonts["speaker"], fill=speaker_color, anchor="lm"
    )

    # Текст реплики
    text_zone_w  = text_right - text_x
    text_y_start = ay + r + 28
    max_text_h   = LEFT_Y2 - text_y_start - PAD

    font_body = _fit_font_to_zone(
        draw, text, fonts["body"], fonts["body_sm"],
        zone_w=text_zone_w, max_h=max_text_h,
    )

    lines  = _wrap_text_pixels(draw, text, font_body, text_zone_w)
    line_h = int(font_body.size * 1.38)
    block_h = len(lines) * line_h + 28

    _draw_rounded_rect(
        draw,
        text_x - 10, text_y_start - 14,
        text_right + 10, text_y_start + block_h,
        radius=14, fill=(0, 0, 0, 160)
    )

    for i, line in enumerate(lines):
        draw.text(
            (text_x, text_y_start + i * line_h),
            line, font=font_body, fill=TEXT_COLOR
        )

    return canvas


def _fit_image(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Вписать изображение в размер с кропом по центру."""
    iw, ih = img.size
    scale = max(target_w / iw, target_h / ih)
    new_w, new_h = int(iw * scale), int(ih * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top  = (new_h - target_h) // 2
    return img.crop((left, top, left + target_w, top + target_h))


def _draw_rounded_rect(draw, x0, y0, x1, y1, radius, fill):
    """Нарисовать прямоугольник с закруглёнными углами."""
    from PIL import ImageDraw as ID
    fill_rgba = fill if len(fill) == 4 else fill + (255,)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=radius, fill=fill_rgba)


def _load_fonts() -> dict:
    """Загрузить шрифты. Фоллбэк на встроенный если нет TTF."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]
    font_path = None
    for p in font_paths:
        if Path(p).exists():
            font_path = p
            break

    font_reg_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ]
    font_reg = None
    for p in font_reg_paths:
        if Path(p).exists():
            font_reg = p
            break

    def load(path, size):
        try:
            return ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    return {
        "title":   load(font_path, 34),
        "speaker": load(font_path, 30),
        "avatar":  load(font_path, 32),
        "body":    load(font_reg or font_path, 40),
        "body_sm": load(font_reg or font_path, 30),
        "caption": load(font_reg or font_path, 24),
    }

def _wrap_text_pixels(draw, text: str, font, max_width: int) -> list[str]:
    """
    Разбивает текст на строки так, чтобы каждая не превышала max_width пикселей.
    Работает корректно для любого языка (кириллица, латиница).
    """
    words = text.split()
    lines = []
    current = ""

    for word in words:
        test = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), test, font=font)
        w = bbox[2] - bbox[0]
        if w <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            # Если одно слово шире зоны — обрезаем посимвольно
            if draw.textbbox((0, 0), word, font=font)[2] > max_width:
                chunk = ""
                for ch in word:
                    test_ch = chunk + ch
                    if draw.textbbox((0, 0), test_ch, font=font)[2] <= max_width:
                        chunk = test_ch
                    else:
                        lines.append(chunk)
                        chunk = ch
                current = chunk
            else:
                current = word

    if current:
        lines.append(current)

    return lines


def _fit_font_to_zone(draw, text: str, font_large, font_small,
                      zone_w: int, max_h: int):
    """
    Возвращает font_large если текст влезает в зону, иначе font_small.
    """
    for font in (font_large, font_small):
        lines = _wrap_text_pixels(draw, text, font, zone_w)
        line_h = int(font.size * 1.35)
        total_h = len(lines) * line_h
        if total_h <= max_h:
            return font
    return font_small  # последний фоллбэк