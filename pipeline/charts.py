"""
Charts from the article's own numbers.

Gemini is asked to pick up to two sets of comparable figures from the source
text; every value is then checked against the text itself — a number that does
not literally appear in the source is thrown away, so a misread or invented
figure can never reach a video. What survives is drawn as a bar chart and
handed to the script writer as one more "image" (kind="chart"), so the existing
image_index mechanism puts it on screen while the hosts talk about it.

Any failure here is non-fatal: the video is simply made without charts.
"""

import copy
import json
import logging
import os
import re
from pathlib import Path

from PIL import Image, ImageDraw

from common import gemini_models
from pipeline.step04_frames import find_fonts, font_at

log = logging.getLogger("charts")

CHART_W, CHART_H = 1800, 700
MAX_CHARTS = 2
MIN_ITEMS, MAX_ITEMS = 3, 8
ENABLED = os.getenv("CHARTS_ENABLED", "1") != "0"
CAPTION_PREFIX = {"ru": "График: ", "en": "Chart: "}
_LANG_NAME = {"ru": "русском", "en": "English"}

BG, PANEL = (12, 12, 18), (22, 22, 32)
ORANGE, CYAN, WHITE, GREY = (255, 122, 40), (64, 196, 255), (245, 245, 250), (150, 150, 170)

_PROMPT = """Ниже — текст статьи. Найди в нём числовые данные, которые стоит показать столбчатым графиком:
сравнение ОДНОЙ метрики (доли, объёмы, цены, рейтинги) между {min_items}–{max_items} объектами.

Верни СТРОГО JSON без markdown и пояснений:
{{"charts": [{{"title": "...", "unit": "%", "items": [{{"label": "...", "value": 67.5}}]}}]}}

Правила:
- не более {max_charts} графиков; если подходящих данных нет — {{"charts": []}};
- берись ТОЛЬКО за числа, которые есть в тексте дословно: ничего не считай, не округляй и не придумывай;
- все значения одного графика в одной единице измерения (unit: "%", "млн", "тыс.", "$" и т.п.);
- заголовок (до 70 символов) и подписи (до 20 символов) — на {lang_name} языке, короткие;

ТЕКСТ СТАТЬИ:
{text}
"""

_NUM = re.compile(r"\d+(?:[.,]\d+)?")


def source_numbers(text: str) -> set[float]:
    """Every number that appears in the text, read both as a decimal ("67,5" ->
    67.5) and as a thousands-grouped integer ("1,023" -> 1023, "186 300" -> 186300)."""
    found = set()
    squeezed = re.sub(r"(?<=\d)[   ](?=\d{3}\b)", "", text)
    for source in (text, squeezed):
        for tok in _NUM.findall(source):
            try:
                found.add(float(tok.replace(",", ".")))
                if "," in tok or "." in tok:
                    found.add(float(tok.replace(",", "").replace(".", "")))
            except ValueError:
                continue
    return found


def validate_spec(raw: dict, text: str) -> dict | None:
    """Returns a clean chart spec made only of figures present in `text`, or None."""
    if not isinstance(raw, dict):
        return None
    unit = str(raw.get("unit") or "").strip()[:8]
    title = " ".join(str(raw.get("title") or "").split())[:70]
    if not title:
        return None
    present = source_numbers(text)
    items, seen = [], set()
    for it in raw.get("items") or []:
        if not isinstance(it, dict):
            continue
        label = " ".join(str(it.get("label") or "").split())[:20]
        try:
            value = float(it.get("value"))
        except (TypeError, ValueError):
            continue
        if not label or label.lower() in seen or value <= 0 or value > 1e12:
            continue
        if unit == "%" and value > 100:
            continue
        if not any(abs(value - n) < 1e-6 for n in present):
            log.info(f"график «{title}»: значение {value} ({label}) не найдено в тексте — пропущено")
            continue
        seen.add(label.lower())
        items.append({"label": label, "value": value})
    if len(items) < MIN_ITEMS:
        return None
    items.sort(key=lambda i: i["value"], reverse=True)
    return {"title": title, "unit": unit, "items": items[:MAX_ITEMS]}


def _format_value(value: float, unit: str) -> str:
    num = f"{int(value):,}".replace(",", " ") if float(value).is_integer() else f"{value:g}"
    if not unit:
        return num
    return num + (unit if unit == "%" else f" {unit}")


def render_bar_chart(spec: dict, path: Path) -> Path:
    bold, reg = find_fonts()
    f_title, f_label, f_value = font_at(bold, 38), font_at(reg, 32), font_at(bold, 34)
    img = Image.new("RGB", (CHART_W, CHART_H), PANEL)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 10, CHART_H), fill=CYAN)
    d.text((50, 22), spec["title"], font=f_title, fill=WHITE)
    d.line((50, 80, CHART_W - 50, 80), fill=(45, 45, 60), width=2)

    items = spec["items"]
    # Fill the area: few bars get taller rows, and the block is centred under the title.
    row_h = min(104, (CHART_H - 110) // len(items))
    bar_h = int(row_h * 0.62)
    top_y = 90 + (CHART_H - 90 - len(items) * row_h) // 2
    label_right, bar_x0 = 380, 400
    values = [_format_value(i["value"], spec["unit"]) for i in items]
    value_w = max(int(d.textlength(v, font=f_value)) for v in values) + 30
    max_bar = CHART_W - bar_x0 - value_w - 50
    top = max(i["value"] for i in items)
    for n, (item, vtext) in enumerate(zip(items, values)):
        y = top_y + n * row_h
        d.text((label_right, y + bar_h // 2), item["label"], font=f_label, fill=GREY, anchor="rm")
        width = max(8, int(max_bar * item["value"] / top))
        d.rounded_rectangle((bar_x0, y, bar_x0 + width, y + bar_h), 12, fill=ORANGE if n == 0 else CYAN)
        d.text((bar_x0 + width + 16, y + bar_h // 2), vtext, font=f_value, fill=WHITE, anchor="lm")
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(path), "PNG")
    return path


def extract_chart_specs(blocks: list[dict], lang: str) -> list[dict]:
    from pipeline.step02_script import _build_sampled_text
    text = _build_sampled_text(blocks, max_chars=15000)
    if len(source_numbers(text)) < MIN_ITEMS:
        return []
    prompt = _PROMPT.format(min_items=MIN_ITEMS, max_items=MAX_ITEMS, max_charts=MAX_CHARTS,
                            lang_name=_LANG_NAME.get(lang, "English"), text=text)
    reply = gemini_models.generate_text(
        prompt, api_key=os.getenv("GEMINI_API_KEY") or gemini_models.api_key(), timeout=60, json_mode=True).text
    reply = re.sub(r"^```(?:json)?\s*|\s*```$", "", reply.strip())
    data = json.loads(reply)
    specs = []
    for raw in (data.get("charts") or [])[:MAX_CHARTS]:
        spec = validate_spec(raw, text)
        if spec:
            specs.append(spec)
    return specs


def add_chart_images(blocks: list[dict], job_dir: Path, lang: str) -> tuple[list[dict], int]:
    """Returns (blocks with chart images appended to the first block, number added).
    Works on a copy: webpage jobs share one `blocks` list between the RU and EN
    jobs, and each language gets its own chart labels."""
    blocks = copy.deepcopy(blocks)
    if not ENABLED or not blocks:
        return blocks, 0
    try:
        specs = extract_chart_specs(blocks, lang)
    except Exception as e:
        log.warning(f"графики пропущены: {type(e).__name__}: {str(e)[:120]}")
        return blocks, 0
    added = 0
    for n, spec in enumerate(specs):
        try:
            path = render_bar_chart(spec, job_dir / "charts" / f"chart_{n}.png")
        except Exception as e:
            log.warning(f"график «{spec['title']}» не нарисован: {e}")
            continue
        blocks[0].setdefault("images", []).append({
            "path": str(path), "caption": CAPTION_PREFIX.get(lang, "Chart: ") + spec["title"],
            "bbox_y": 1e6 + n, "kind": "chart",
        })
        added += 1
    if added:
        log.info(f"Добавлено графиков: {added}")
    return blocks, added
