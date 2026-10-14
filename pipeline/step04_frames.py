"""
Шаг 4: Сборка кадров видео 1920x1080.

Раскладка: заголовок сверху, большая область визуала (фото / график / карточка
с тезисами), снизу — одна панель субтитров и полоса прогресса.

Длинная реплика не помещается в панель целиком, поэтому субтитры «катятся»:
на экране предыдущая строка (приглушённая) и текущая. Каждая строка получает
свой кадр на долю времени реплики пропорционально длине текста — так нет
«хвостов» из одного-двух слов, субтитры читаемы, а картинка постоянно
обновляется. Сумма длительностей равна длительности реплики, так что
синхронизация со звуком не страдает.
"""

import hashlib
import logging
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

log = logging.getLogger("step04")

W, H = 1920, 1080
MARGIN = 60

VIS_BOX   = (MARGIN, 72, W - MARGIN, 772)        # 1800 x 700
PANEL_BOX = (MARGIN, 796, W - MARGIN, 1020)
VIS_W, VIS_H = VIS_BOX[2] - VIS_BOX[0], VIS_BOX[3] - VIS_BOX[1]
TEXT_X0, TEXT_X1 = 100, W - 100
SUB_SIZE, LINE_H = 44, 58
DIM = (125, 125, 145)
MIN_CHUNK_SECONDS = 1.0
COVER_MIN_ASPECT = 1.9        # wide images fill the area; others sit on a blurred backdrop

# Figures and pages taken from papers are zoomed in steps across the subtitle
# lines of the reply that shows them (1.0 switches zooming off).
ZOOM_KINDS = {"figure", "page"}
ZOOM_MAX = float(os.getenv("VISUAL_ZOOM_MAX", "1.5"))
PAGE_MAX_SCALE = 3.0

BG_COLOR   = (12, 12, 18)
PANEL_FILL = (22, 22, 32)
TEXT_COLOR = (245, 245, 250)
GREY       = (150, 150, 170)
HOST1_COLOR = (64, 196, 255)
HOST2_COLOR = (255, 122, 40)

SPEAKER_LABELS = {
    "ru": {"host1": "Ведущий 1", "host2": "Ведущий 2"},
    "en": {"host1": "Host 1",    "host2": "Host 2"},
}
KEY_POINTS_HEADING = {"ru": "Ключевые тезисы", "en": "Key points"}

_BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arialbd.ttf",
]
_REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "C:/Windows/Fonts/arial.ttf",
]


def find_fonts() -> tuple[str | None, str | None]:
    bold = next((p for p in _BOLD_PATHS if Path(p).exists()), None)
    reg = next((p for p in _REG_PATHS if Path(p).exists()), None)
    return bold, reg or bold


def _fallback_font(size):
    # Pillow's built-in font has no Cyrillic: Russian text renders as boxes.
    # Say so loudly instead of silently shipping unreadable frames.
    log.error("Не найден TTF-шрифт с кириллицей (нужен fonts-dejavu-core) — текст на кадрах будет нечитаемым")
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def font_at(path, size):
    try:
        return ImageFont.truetype(path, size) if path else _fallback_font(size)
    except Exception:
        return _fallback_font(size)


def _load_fonts() -> dict:
    bold, reg = find_fonts()
    return {
        "title":  font_at(reg, 30),
        "chip":   font_at(bold, 28),
        "sub":    font_at(reg, SUB_SIZE),
        "cap":    font_at(reg, 24),
        "head":   font_at(bold, 32),
        "bullet": font_at(reg, 46),
        "big":    font_at(bold, 66),
    }


def _measure():
    return ImageDraw.Draw(Image.new("RGB", (1, 1)))


def _wrap_text_pixels(draw, text: str, font, max_width: int) -> list[str]:
    """Разбивает текст на строки не шире max_width пикселей (кириллица и латиница)."""
    words = text.split()
    lines = []
    current = ""
    for word in words:
        test = (current + " " + word).strip()
        if draw.textbbox((0, 0), test, font=font)[2] <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            if draw.textbbox((0, 0), word, font=font)[2] > max_width:
                chunk = ""
                for ch in word:
                    if draw.textbbox((0, 0), chunk + ch, font=font)[2] <= max_width:
                        chunk += ch
                    else:
                        lines.append(chunk)
                        chunk = ch
                current = chunk
            else:
                current = word
    if current:
        lines.append(current)
    return lines


def _ellipsize(draw, text: str, font, max_width: int) -> str:
    if draw.textbbox((0, 0), text, font=font)[2] <= max_width:
        return text
    while text and draw.textbbox((0, 0), text + "…", font=font)[2] > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _rounded(im: Image.Image, radius: int) -> Image.Image:
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, im.size[0] - 1, im.size[1] - 1), radius, fill=255)
    out = im.convert("RGBA")
    out.putalpha(mask)
    return out


# ── Область визуала ──────────────────────────────────────────────────────────
def _backdrop(img: Image.Image) -> Image.Image:
    """Blurred, darkened copy of the picture filling the visual area."""
    iw, ih = img.size
    cover = max(VIS_W / iw, VIS_H / ih)
    back = img.resize((int(iw * cover) + 1, int(ih * cover) + 1), Image.BILINEAR)
    left, top = (back.width - VIS_W) // 2, (back.height - VIS_H) // 2
    back = back.crop((left, top, left + VIS_W, top + VIS_H)).filter(ImageFilter.GaussianBlur(30))
    return Image.eval(back, lambda v: int(v * 0.4))


def _visual_from_image(path: str) -> Image.Image:
    img = Image.open(path).convert("RGB")
    iw, ih = img.size
    if iw / ih >= COVER_MIN_ASPECT:
        scale = max(VIS_W / iw, VIS_H / ih)
        img = img.resize((int(iw * scale) + 1, int(ih * scale) + 1), Image.LANCZOS)
        left, top = (img.width - VIS_W) // 2, (img.height - VIS_H) // 2
        return img.crop((left, top, left + VIS_W, top + VIS_H))
    # Tall or square pictures (figures, diagrams, portraits) must not be cropped:
    # show them whole, centred over a blurred, darkened copy of themselves.
    back = _backdrop(img)
    fit = min(VIS_W / iw, VIS_H / ih)
    front = img.resize((max(1, int(iw * fit)), max(1, int(ih * fit))), Image.LANCZOS)
    back.paste(front, ((VIS_W - front.width) // 2, (VIS_H - front.height) // 2))
    return back


def _content_box(img: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of everything that differs from the page background, so
    white margins around a figure or page are not shown (or zoomed into)."""
    bg = Image.new(img.mode, img.size, img.getpixel((0, 0)))
    mask = ImageChops.difference(img, bg).convert("L").point(lambda v: 255 if v > 24 else 0)
    box = mask.getbbox()
    if not box:
        return 0, 0, img.width, img.height
    pad_x, pad_y = int(img.width * 0.012), int(img.height * 0.012)
    return (max(0, box[0] - pad_x), max(0, box[1] - pad_y),
            min(img.width, box[2] + pad_x), min(img.height, box[3] + pad_y))


def _clamp_center(center: float, view: float, lo: float, hi: float) -> float:
    if view >= hi - lo:                 # the view is larger than the content: centre it
        return (lo + hi) / 2
    return min(max(center, lo + view / 2), hi - view / 2)


class _Camera:
    """A virtual camera over one paper figure or page. Window k of n (one per
    subtitle line) gets its own view: a figure starts whole, then zooms in and
    tours along its long side so details appear while the hosts talk about them;
    a text page scrolls from top to bottom at a readable size."""

    def __init__(self, path: str, kind: str):
        self.kind = kind
        self.src = Image.open(path).convert("RGB")
        self.box = _content_box(self.src)
        self.backdrop = _backdrop(self.src)

    def view_rect(self, k: int, n: int) -> tuple[float, float, float, float, float]:
        """(x0, y0, x1, y1, scale) of the view in source pixels for window k of n."""
        bx0, by0, bx1, by1 = self.box
        cw, ch = bx1 - bx0, by1 - by0
        if self.kind == "page":
            scale = min(VIS_W / cw * 0.98, PAGE_MAX_SCALE)
            vw, vh = VIS_W / scale, VIS_H / scale
            t = k / (n - 1) if n > 1 else 0.0
            cx = _clamp_center((bx0 + bx1) / 2, vw, bx0, bx1)
            cy = _clamp_center(by0 + vh / 2 + max(0.0, ch - vh) * t, vh, by0, by1)
        else:
            fit = min(VIS_W / cw, VIS_H / ch)
            zoom = 1.0 if (k == 0 or n == 1) else ZOOM_MAX
            scale = fit * zoom
            vw, vh = VIS_W / scale, VIS_H / scale
            cx, cy = (bx0 + bx1) / 2, (by0 + by1) / 2
            if zoom > 1.0:
                steps = n - 1
                t = (k - 1) / (steps - 1) if steps > 1 else 0.5
                travel_x, travel_y = max(0.0, cw - vw), max(0.0, ch - vh)
                if travel_x >= travel_y and travel_x > 0:
                    cx = bx0 + vw / 2 + travel_x * t
                elif travel_y > 0:
                    cy = by0 + vh / 2 + travel_y * t
            cx = _clamp_center(cx, vw, bx0, bx1)
            cy = _clamp_center(cy, vh, by0, by1)
        return cx - vw / 2, cy - vh / 2, cx + vw / 2, cy + vh / 2, scale

    def render(self, k: int, n: int) -> Image.Image:
        vx0, vy0, vx1, vy1, scale = self.view_rect(k, n)
        bx0, by0, bx1, by1 = self.box
        ix0, iy0, ix1, iy1 = max(vx0, bx0), max(vy0, by0), min(vx1, bx1), min(vy1, by1)
        canvas = self.backdrop.copy()
        w, h = max(1, round((ix1 - ix0) * scale)), max(1, round((iy1 - iy0) * scale))
        piece = self.src.resize((w, h), Image.LANCZOS, box=(ix0, iy0, ix1, iy1))
        canvas.paste(piece, (round((ix0 - vx0) * scale), round((iy0 - vy0) * scale)))
        return canvas


def _visual_key_points(title: str, key_points: list[str], fonts: dict, lang: str) -> Image.Image:
    """Shown when there is no picture: the episode's key points instead of a dead area."""
    vis = Image.new("RGB", (VIS_W, VIS_H), PANEL_FILL)
    d = ImageDraw.Draw(vis)
    for y in range(VIS_H):                         # soft vertical gradient
        shade = int(8 * y / VIS_H)
        d.line([(0, y), (VIS_W, y)], fill=(PANEL_FILL[0] - shade, PANEL_FILL[1] - shade, PANEL_FILL[2] - shade + 4))
    d.rectangle((0, 0, 10, VIS_H), fill=HOST1_COLOR)
    points = [p.strip() for p in key_points if p and p.strip()][:3]
    if not points:
        lines = _wrap_text_pixels(d, title, fonts["big"], VIS_W - 260)[:4]
        y = (VIS_H - len(lines) * 84) // 2
        for ln in lines:
            d.text((VIS_W // 2, y), ln, font=fonts["big"], fill=TEXT_COLOR, anchor="ma")
            y += 84
        return vis
    d.text((90, 60), KEY_POINTS_HEADING.get(lang, KEY_POINTS_HEADING["en"]), font=fonts["head"], fill=GREY)
    y = 150
    for i, point in enumerate(points):
        color = HOST1_COLOR if i % 2 == 0 else HOST2_COLOR
        d.ellipse((90, y + 14, 120, y + 44), fill=color)
        for ln in _wrap_text_pixels(d, point, fonts["bullet"], VIS_W - 300)[:2]:
            d.text((150, y), ln, font=fonts["bullet"], fill=TEXT_COLOR)
            y += 60
        y += 52
    return vis


def _base_frame(visual: Image.Image, title: str, caption: str, show_caption: bool, fonts: dict) -> Image.Image:
    canvas = Image.new("RGB", (W, H), BG_COLOR)
    d = ImageDraw.Draw(canvas)
    if title:
        d.text((MARGIN, 26), _ellipsize(d, title, fonts["title"], W - 2 * MARGIN), font=fonts["title"], fill=GREY)
    vis = _rounded(visual, 28)
    if show_caption and caption:
        overlay = Image.new("RGBA", vis.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        text = _ellipsize(od, " ".join(caption.split()), fonts["cap"], 1300)
        width = int(od.textlength(text, font=fonts["cap"])) + 36
        od.rounded_rectangle((24, VIS_H - 70, 24 + width, VIS_H - 24), 14, fill=(0, 0, 0, 165))
        od.text((42, VIS_H - 47), text, font=fonts["cap"], fill=(235, 235, 240), anchor="lm")
        vis = Image.alpha_composite(vis, overlay)
    canvas.paste(vis, (VIS_BOX[0], VIS_BOX[1]), vis)
    d.rounded_rectangle(PANEL_BOX, 28, fill=PANEL_FILL)
    return canvas


# ── Субтитры ─────────────────────────────────────────────────────────────────
def subtitle_windows(text: str, font, draw=None) -> list[tuple[str, str]]:
    """One window per line of text: (previous line, current line)."""
    draw = draw or _measure()
    lines = _wrap_text_pixels(draw, " ".join(text.split()), font, TEXT_X1 - TEXT_X0)
    return [(lines[i - 1] if i else "", line) for i, line in enumerate(lines)] or [("", "")]


def chunk_durations(chunks: list[str], total_ms: int) -> list[int]:
    """Splits total_ms across lines by text length, never below MIN_CHUNK_SECONDS
    (when the segment is long enough), always summing exactly to total_ms."""
    n = len(chunks)
    if n == 1:
        return [total_ms]
    weights = [max(len(c.replace("\n", " ")), 1) for c in chunks]
    floor = min(int(MIN_CHUNK_SECONDS * 1000), total_ms // n)
    spare = total_ms - floor * n
    raw = [floor + spare * w // sum(weights) for w in weights]
    raw[-1] += total_ms - sum(raw)
    return raw


def _draw_chunk(base: Image.Image, speaker: str, label: str, prev: str, cur: str,
                progress: float, fonts: dict) -> Image.Image:
    frame = base.copy()
    d = ImageDraw.Draw(frame)
    color = HOST1_COLOR if speaker == "host1" else HOST2_COLOR
    chip_w = int(d.textlength(label, font=fonts["chip"])) + 56
    d.rounded_rectangle((TEXT_X0, 818, TEXT_X0 + chip_w, 866), 24, fill=color)
    d.text((TEXT_X0 + chip_w // 2, 842), label, font=fonts["chip"], fill=(10, 10, 15), anchor="mm")
    if prev:                       # roll-up: the previous line stays above, dimmed
        d.text((TEXT_X0, 886), prev, font=fonts["sub"], fill=DIM)
        d.text((TEXT_X0, 886 + LINE_H), cur, font=fonts["sub"], fill=TEXT_COLOR)
    else:                          # the first line starts in the top row and stays put
        d.text((TEXT_X0, 886), cur, font=fonts["sub"], fill=TEXT_COLOR)
    bar_w = W - 2 * MARGIN
    d.rectangle((MARGIN, 1038, W - MARGIN, 1046), fill=(45, 45, 60))
    d.rectangle((MARGIN, 1038, MARGIN + int(bar_w * max(0.0, min(1.0, progress))), 1046), fill=color)
    return frame


# ── Публичный интерфейс ──────────────────────────────────────────────────────
def build_frames(script: dict, timeline: list[dict], job_dir: Path, lang: str = "ru") -> Path:
    """
    Создаёт PNG-кадры (несколько на реплику, по фрагментам субтитров) и записывает
    их в timeline[i]["frames"] = [{"path", "duration_ms"}]; timeline[i]["frame_path"]
    — первый кадр (для обратной совместимости). Возвращает папку frames/.
    """
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(exist_ok=True)

    fonts = _load_fonts()
    # anchor="mm" raises ValueError on multiline text; collapse whitespace in the title.
    title = " ".join(script.get("title", "").split())
    key_points = script.get("key_points", [])
    labels = SPEAKER_LABELS.get(lang, SPEAKER_LABELS["en"])
    total_ms = max((e.get("end_ms", 0) for e in timeline), default=0)
    measure = _measure()

    last_photo = None        # (path, caption): a photo stays up until the next one replaces it
    cameras: dict[str, _Camera] = {}
    for entry in timeline:
        path, caption, kind = entry.get("image_path"), entry.get("image_caption", ""), entry.get("image_kind")
        if path and Path(str(path)).exists():
            if kind != "chart":
                last_photo = (path, caption)
        elif last_photo:
            path, caption, kind = last_photo[0], last_photo[1], None
        else:
            path = None
        if kind == "chart" and not (path and Path(str(path)).exists()):
            kind = None

        text = " ".join(entry["text"].split())
        windows = subtitle_windows(text, fonts["sub"], measure)
        durations = chunk_durations([cur for _, cur in windows], entry.get("duration_ms", 1000 * len(windows)))
        seg_key = hashlib.sha1("|".join(str(x) for x in (lang, title, path, caption, kind,
                                                         tuple(key_points))).encode("utf-8")).hexdigest()[:8]

        zoom = (kind in ZOOM_KINDS and ZOOM_MAX > 1.0 and bool(path) and Path(str(path)).exists())
        base = None
        elapsed = 0
        frames = []
        for idx, ((prev_line, cur_line), dur) in enumerate(zip(windows, durations)):
            progress = (entry.get("start_ms", 0) + elapsed + dur / 2) / total_ms if total_ms else 0.0
            key = hashlib.sha1(f"{seg_key}|{entry['speaker']}|{prev_line}|{cur_line}|{round(progress * 200)}|{kind if zoom else ''}|{idx}/{len(windows)}|{ZOOM_MAX if zoom else ''}".encode("utf-8")).hexdigest()[:10]
            frame_path = frames_dir / f"frame_{entry['seg_id']:04d}_{idx:02d}_{key}.png"
            frames.append({"path": str(frame_path), "duration_ms": dur})
            elapsed += dur
            if frame_path.exists():
                continue                                  # кэш
            if base is None or zoom:
                try:
                    if zoom:
                        cam = cameras.get(str(path)) or cameras.setdefault(str(path), _Camera(str(path), kind))
                        visual = cam.render(idx, len(windows))
                    else:
                        visual = _visual_from_image(str(path)) if path else _visual_key_points(title, key_points, fonts, lang)
                except Exception as e:
                    log.warning(f"Не удалось загрузить изображение {path}: {e}")
                    visual = _visual_key_points(title, key_points, fonts, lang)
                # A zoomed figure is looked at closely, so its caption goes to the top bar
                # instead of an overlay chip that would cover part of the picture.
                top_text = " ".join(caption.split()) if (zoom and caption) else title
                base = _base_frame(visual, top_text, caption, kind != "chart" and bool(path) and not zoom, fonts)
            img = _draw_chunk(base, entry["speaker"], labels.get(entry["speaker"], entry["speaker"]),
                              prev_line, cur_line, progress, fonts)
            # Write-then-rename: a process killed mid-save must not leave a
            # truncated PNG that later runs would reuse as "cached".
            tmp_path = frame_path.with_name(frame_path.name + ".tmp")
            img.save(str(tmp_path), "PNG", compress_level=1)
            os.replace(tmp_path, frame_path)
        entry["frames"] = frames
        entry["frame_path"] = frames[0]["path"]
        log.info(f"Реплика {entry['seg_id'] + 1}/{len(timeline)}: {len(frames)} кадр(ов)")

    log.info(f"Все кадры → {frames_dir}")
    return frames_dir
