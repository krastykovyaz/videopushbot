"""
Шаг 6: Генерация YouTube-стиль thumbnail 1280x720.
Большой заголовок + первая картинка из статьи как фон.
"""

import logging
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

log = logging.getLogger("step06")

TW, TH = 1280, 720   # размер thumbnail

# Палитра
GRAD_LEFT  = (10, 10, 18)
GRAD_RIGHT = (24, 16, 48)
ACCENT     = (99, 102, 241)    # indigo
TEXT_WHITE = (255, 255, 255)
TEXT_DIM   = (180, 180, 200)
BADGE_BG   = (99, 102, 241, 220)


def make_thumbnail(script: dict, blocks: list[dict], job_dir: Path) -> Path:
    """
    Создаёт thumbnail.png.
    Возвращает путь к файлу.
    """
    out_path = job_dir / "thumbnail.png"
    title       = script.get("title", "Untitled")
    key_points  = script.get("key_points", [])[:3]

    # Найти первое подходящее изображение из PDF
    bg_image_path = _find_best_image(blocks)

    canvas = _render_thumbnail(title, key_points, bg_image_path)
    canvas.save(str(out_path), "PNG", optimize=True)
    log.info(f"Thumbnail → {out_path}")
    return out_path


def _render_thumbnail(title: str, key_points: list, bg_image_path) -> Image.Image:
    canvas = Image.new("RGB", (TW, TH), GRAD_LEFT)
    draw = ImageDraw.Draw(canvas)

    # ── Градиентный фон ───────────────────────────────────────────────────
    for x in range(TW):
        t = x / TW
        r = int(GRAD_LEFT[0] + (GRAD_RIGHT[0] - GRAD_LEFT[0]) * t)
        g = int(GRAD_LEFT[1] + (GRAD_RIGHT[1] - GRAD_LEFT[1]) * t)
        b = int(GRAD_LEFT[2] + (GRAD_RIGHT[2] - GRAD_LEFT[2]) * t)
        draw.line([(x, 0), (x, TH)], fill=(r, g, b))

    # ── Фоновое изображение (правая половина, с блюром и прозрачностью) ───
    if bg_image_path:
        try:
            bg = Image.open(bg_image_path).convert("RGB")
            # Вписать в правую половину
            target_w, target_h = int(TW * 0.65), TH
            iw, ih = bg.size
            scale = max(target_w / iw, target_h / ih)
            bg = bg.resize((int(iw * scale), int(ih * scale)), Image.LANCZOS)
            # Кроп
            bw, bh = bg.size
            bg = bg.crop(((bw - target_w) // 2, 0, (bw + target_w) // 2, target_h))
            # Лёгкий блур для киношного вида
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2))
            # Наложить справа с альфой
            bg_rgba = bg.convert("RGBA")
            # Градиент прозрачности: левый край прозрачный, правый — полный
            alpha_mask = Image.new("L", (target_w, TH), 0)
            alpha_draw = ImageDraw.Draw(alpha_mask)
            for x in range(target_w):
                alpha = int(180 * min(1.0, x / (target_w * 0.4)))
                alpha_draw.line([(x, 0), (x, TH)], fill=alpha)
            bg_rgba.putalpha(alpha_mask)
            canvas_rgba = canvas.convert("RGBA")
            canvas_rgba.paste(bg_rgba, (int(TW * 0.35), 0), bg_rgba)
            canvas = canvas_rgba.convert("RGB")
            draw = ImageDraw.Draw(canvas)
        except Exception as e:
            log.warning(f"Thumbnail: не удалось загрузить фон: {e}")

    fonts = _load_fonts()

    # ── Декоративная вертикальная линия ───────────────────────────────────
    draw.rectangle([48, 60, 54, TH - 60], fill=ACCENT)

    # ── Бейдж "AI Podcast" сверху ─────────────────────────────────────────
    badge_text = "  AI PODCAST  "
    bbox = draw.textbbox((0, 0), badge_text, font=fonts["badge"])
    bw = bbox[2] - bbox[0]
    draw.rounded_rectangle([70, 48, 70 + bw + 16, 90], radius=8, fill=BADGE_BG)
    draw.text((78, 56), badge_text, font=fonts["badge"], fill=TEXT_WHITE)

    # ── Главный заголовок ────────────────────────────────────────────────
    # Разбить на строки, макс 22 символа в строке для крупного шрифта
    title_lines = textwrap.wrap(title[:80], width=22)[:3]
    title_y = 120
    line_h = 115

    for line in title_lines:
        # Тень
        draw.text((74, title_y + 3), line, font=fonts["title"], fill=(0, 0, 0, 120))
        draw.text((72, title_y), line, font=fonts["title"], fill=TEXT_WHITE)
        title_y += line_h

    # ── Ключевые тезисы ───────────────────────────────────────────────────
    if key_points:
        kp_y = max(title_y + 30, TH - 220)
        for i, point in enumerate(key_points):
            # Иконка
            dot_x, dot_y = 72, kp_y + i * 56 + 10
            draw.ellipse([dot_x, dot_y, dot_x + 18, dot_y + 18], fill=ACCENT)
            draw.text((dot_x + 9, dot_y + 9), str(i+1),
                      font=fonts["dot"], fill=TEXT_WHITE, anchor="mm")
            # Текст тезиса
            pt = point[:60] + ("…" if len(point) > 60 else "")
            draw.text((102, kp_y + i * 56), pt, font=fonts["point"], fill=TEXT_DIM)

    # ── Нижняя полоска ────────────────────────────────────────────────────
    draw.rectangle([0, TH - 8, TW, TH], fill=ACCENT)

    return canvas


def _find_best_image(blocks: list[dict]):
    """Найти первое подходящее изображение (не слишком маленькое)."""
    for block in blocks:
        for img in block.get("images", []):
            p = img.get("path")
            if p and Path(p).exists():
                try:
                    with Image.open(p) as im:
                        if im.width >= 200 and im.height >= 150:
                            return p
                except Exception:
                    continue
    return None


def _load_fonts() -> dict:
    font_paths_bold = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arialbd.ttf",
    ]
    font_paths_reg = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
        "C:/Windows/Fonts/arial.ttf",
    ]

    def find(paths):
        for p in paths:
            if Path(p).exists():
                return p
        return None

    bold = find(font_paths_bold)
    reg  = find(font_paths_reg)

    def load(path, size):
        try:
            return ImageFont.truetype(path, size) if path else ImageFont.load_default()
        except Exception:
            return ImageFont.load_default()

    return {
        "title": load(bold, 96),
        "badge": load(bold, 22),
        "point": load(reg,  30),
        "dot":   load(bold, 14),
    }
