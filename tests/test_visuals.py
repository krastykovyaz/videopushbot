import json
from pathlib import Path

import pytest
from PIL import Image

from pipeline import charts, step04_frames as s4, step05_video


# ── chart data must come from the source text ─────────────────────────────────
SOURCE = ("Новые авто: Китай — 67,5%, Киргизия — 20,5%, Беларусь — 4%, Южная Корея — 2%. "
          "Всего ввезено 186 300 машин, из них подержанных 82,3 тыс.")


def spec(items, unit="%", title="Откуда везут новые авто"):
    return {"title": title, "unit": unit, "items": [{"label": l, "value": v} for l, v in items]}


def test_source_numbers_reads_decimals_and_grouped_thousands():
    nums = charts.source_numbers(SOURCE)
    assert {67.5, 20.5, 4.0, 2.0, 186300.0, 82.3} <= nums
    assert 1023.0 in charts.source_numbers("выручка 1,023 млн")      # thousands comma


def test_values_not_in_the_text_are_dropped():
    raw = spec([("Китай", 67.5), ("Киргизия", 20.5), ("Беларусь", 4), ("Япония", 9.9)])   # 9.9 is invented
    out = charts.validate_spec(raw, SOURCE)
    assert [i["label"] for i in out["items"]] == ["Китай", "Киргизия", "Беларусь"]


def test_chart_needs_three_verified_items():
    assert charts.validate_spec(spec([("Китай", 67.5), ("Киргизия", 20.5), ("Япония", 9.9)]), SOURCE) is None


def test_bad_items_are_rejected():
    raw = spec([("Китай", 67.5), ("Китай", 67.5), ("Киргизия", 20.5), ("", 4), ("Беларусь", -4),
                ("Корея", 2), ("Х", "n/a")])
    out = charts.validate_spec(raw, SOURCE)
    labels = [i["label"] for i in out["items"]]
    assert labels == ["Китай", "Киргизия", "Корея"]                   # duplicate/empty/negative/non-numeric gone
    assert charts.validate_spec({"title": "", "items": []}, SOURCE) is None
    assert charts.validate_spec("junk", SOURCE) is None


def test_percentages_over_100_are_rejected():
    assert charts.validate_spec(spec([("A", 186300), ("B", 67.5), ("C", 20.5), ("D", 4)]), SOURCE)["items"][0]["label"] == "B"


def test_chart_renders_at_the_visual_area_size(tmp_path):
    clean = charts.validate_spec(spec([("Китай", 67.5), ("Киргизия", 20.5), ("Беларусь", 4), ("Корея", 2)]), SOURCE)
    path = charts.render_bar_chart(clean, tmp_path / "c.png")
    assert Image.open(path).size == (charts.CHART_W, charts.CHART_H) == (s4.VIS_W, s4.VIS_H)


def test_chart_failure_never_breaks_the_job(tmp_path, monkeypatch):
    blocks = [{"page": 0, "text": SOURCE, "images": []}]
    def boom(*a, **k): raise RuntimeError("Gemini down")
    monkeypatch.setattr(charts, "extract_chart_specs", boom)
    out, n = charts.add_chart_images(blocks, tmp_path, "ru")
    assert n == 0 and out == blocks and out is not blocks


def test_charts_are_added_to_a_copy_per_language(tmp_path, monkeypatch):
    shared = [{"page": 0, "text": SOURCE, "images": [{"path": "p.jpg", "caption": "photo"}]}]
    clean = charts.validate_spec(spec([("Китай", 67.5), ("Киргизия", 20.5), ("Беларусь", 4)]), SOURCE)
    monkeypatch.setattr(charts, "extract_chart_specs", lambda b, l: [clean])
    ru, n = charts.add_chart_images(shared, tmp_path / "ru", "ru")
    en, _ = charts.add_chart_images(shared, tmp_path / "en", "en")
    assert n == 1 and len(shared[0]["images"]) == 1                    # the shared list is untouched
    assert ru[0]["images"][-1]["kind"] == "chart" and ru[0]["images"][-1]["caption"].startswith("График: ")
    assert en[0]["images"][-1]["caption"].startswith("Chart: ")


# ── subtitle chunks ───────────────────────────────────────────────────────────
def test_chunk_durations_sum_exactly_and_respect_the_floor():
    chunks = ["a" * 90, "b" * 90, "c" * 6]
    for total in (3000, 12345, 40000):
        d = s4.chunk_durations(chunks, total)
        assert sum(d) == total and len(d) == 3
        assert min(d) >= min(1000, total // 3)
    assert s4.chunk_durations(["x"], 7777) == [7777]


def test_subtitles_roll_one_line_at_a_time():
    font = s4._load_fonts()["sub"]
    text = " ".join(["слово"] * 120)
    windows = s4.subtitle_windows(text, font)
    assert len(windows) > 2 and windows[0][0] == ""
    assert all(prev == windows[i - 1][1] for i, (prev, _) in enumerate(windows) if i)   # previous line stays above
    assert all("\n" not in cur for _, cur in windows)
    assert " ".join(cur for _, cur in windows) == text                    # nothing lost or reordered
    assert s4.subtitle_windows("", font) == [("", "")]


# ── frames + assembly ─────────────────────────────────────────────────────────
def _photo(path, size):
    Image.new("RGB", size, (200, 80, 40)).save(path)
    return str(path)


def _timeline(path=None, kind=None):
    return [{"seg_id": 0, "speaker": "host1", "text": " ".join(["слово"] * 60), "start_ms": 0, "end_ms": 20000,
             "duration_ms": 20000, "image_path": path, "image_caption": "очень " * 30 + "длинная подпись", "image_kind": kind},
            {"seg_id": 1, "speaker": "host2", "text": "Короткий ответ.", "start_ms": 20300, "end_ms": 23000,
             "duration_ms": 2700, "image_path": None, "image_caption": ""}]


@pytest.mark.parametrize("size", [(1600, 600), (800, 800), (600, 1200)])
def test_frames_render_for_wide_square_and_tall_images(tmp_path, size):
    tl = _timeline(_photo(tmp_path / "p.png", size))
    s4.build_frames({"title": "Заголовок", "key_points": ["a", "b"]}, tl, tmp_path, "ru")
    for entry in tl:
        assert entry["frames"] and all(Path(f["path"]).exists() for f in entry["frames"])
        assert sum(f["duration_ms"] for f in entry["frames"]) == entry["duration_ms"]
        assert Image.open(entry["frames"][0]["path"]).size == (1920, 1080)
    assert len(tl[0]["frames"]) > 1                                       # the long reply became several frames


def _visual(entry):
    return Image.open(entry["frames"][0]["path"]).crop(s4.VIS_BOX)


def test_no_picture_anywhere_still_renders_a_key_points_card(tmp_path):
    tl = _timeline(None)
    s4.build_frames({"title": "Заголовок", "key_points": ["тезис один", "тезис два"]}, tl, tmp_path, "ru")
    photo_tl = _timeline(_photo(tmp_path / "p.png", (1600, 600)))
    s4.build_frames({"title": "Заголовок", "key_points": ["тезис один", "тезис два"]}, photo_tl, tmp_path / "other", "ru") \
        if (tmp_path / "other").mkdir() is None else None
    assert _visual(tl[0]).tobytes() != _visual(photo_tl[0]).tobytes()      # a card, not the photo
    assert len({_visual(tl[0]).getpixel((x, 300)) for x in range(100, 1700, 40)}) > 1   # and not a blank fill


def test_a_photo_stays_up_for_following_replies_without_one(tmp_path):
    tl = _timeline(_photo(tmp_path / "p.png", (1600, 600)))
    assert tl[1]["image_path"] is None
    s4.build_frames({"title": "T", "key_points": ["x"]}, tl, tmp_path, "ru")
    assert _visual(tl[0]).tobytes() == _visual(tl[1]).tobytes()           # same photo, no flicker to a card


def test_a_chart_is_not_carried_into_the_next_reply(tmp_path):
    chart = tmp_path / "chart.png"
    charts.render_bar_chart(charts.validate_spec(spec([("Китай", 67.5), ("Киргизия", 20.5), ("Беларусь", 4)]), SOURCE), chart)
    tl = _timeline(str(chart), "chart")
    s4.build_frames({"title": "T", "key_points": ["тезис"]}, tl, tmp_path, "ru")
    first = Image.open(tl[0]["frames"][0]["path"]).crop((60, 72, 1860, 772))
    second = Image.open(tl[1]["frames"][0]["path"]).crop((60, 72, 1860, 772))
    assert first.tobytes() != second.tobytes()                            # next reply shows key points, not the chart


def test_concat_uses_every_frame_with_the_same_total_time(tmp_path):
    frames = tmp_path / "frames"; frames.mkdir()
    paths = []
    for i in range(3):
        p = frames / f"f{i}.png"; p.write_bytes(b"x"); paths.append(p)
    timeline = [{"seg_id": 0, "duration_ms": 6000,
                 "frames": [{"path": str(paths[0]), "duration_ms": 2000}, {"path": str(paths[1]), "duration_ms": 4000}]},
                {"seg_id": 1, "duration_ms": 3000, "frames": [{"path": str(paths[2]), "duration_ms": 3000}]}]
    step05_video._write_concat(timeline, frames, tmp_path / "concat.txt")
    durations = [float(l.split()[1]) for l in (tmp_path / "concat.txt").read_text().splitlines() if l.startswith("duration")]
    assert durations == [2.0, 4.3, 3.3]                                   # +0.3 s pause only after each reply
    assert abs(sum(durations) - (6 + 3 + 0.6)) < 1e-9


# ── paper figures: extraction and camera zoom ─────────────────────────────────
def _paper_pdf(path, figures=1, pages=1):
    import fitz
    doc = fitz.open()
    for p in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), "Introduction. " + "Some body text of the paper. " * 4, fontsize=10)
        for f in range(figures):
            y = 120 + f * 300
            for i in range(5):                                    # a boxes-and-arrows diagram
                page.draw_rect(fitz.Rect(70 + i * 95, y, 135 + i * 95, y + 60), color=(0, 0, 0.6), fill=(0.8, 0.9, 1))
                if i:
                    page.draw_line((70 + i * 95 - 30, y + 30), (70 + i * 95, y + 30), color=(0, 0, 0))
            page.draw_rect(fitz.Rect(70, y + 90, 500, y + 170), color=(0.6, 0, 0), fill=(1, 0.9, 0.9))
            page.insert_text((70, y + 195), f"Fig. {f + 1}. Pipeline of the dispatch system", fontsize=9)
    doc.save(str(path))
    return path


def test_vector_figure_is_cropped_with_its_caption(tmp_path):
    from pipeline import step01_extract
    pages = step01_extract.extract_pdf(_paper_pdf(tmp_path / "paper.pdf", figures=3), tmp_path / "job")
    images = [i for p in pages for i in p["images"]]
    figures = [i for i in images if i["kind"] == "figure"]
    assert len(figures) >= 3
    assert all(Image.open(i["path"]).width >= 1500 for i in figures)       # sharp enough to zoom into
    assert any("Pipeline of the dispatch" in i["caption"] for i in figures)
    assert not any(i["kind"] == "page" for i in images)                    # enough figures: no whole-page fallback


def test_paper_without_figures_falls_back_to_scrolled_pages(tmp_path):
    import fitz
    from pipeline import step01_extract
    doc = fitz.open()
    for _ in range(4):                                                     # text pages with only tiny rules/bullets
        page = doc.new_page(width=595, height=842)
        page.insert_text((60, 70), "Body text of the paper. " * 6, fontsize=10)
        for i in range(14):
            page.draw_rect(fitz.Rect(60, 150 + i * 40, 70, 160 + i * 40), color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(str(tmp_path / "text.pdf"))
    pages = step01_extract.extract_pdf(tmp_path / "text.pdf", tmp_path / "job")
    images = [i for p in pages for i in p["images"]]
    assert len(images) == step01_extract.MIN_VISUALS
    assert {i["kind"] for i in images} == {"page"} and all(i["caption"].startswith("Страница") for i in images)


def _zoom_source(tmp_path, size, name="z.png"):
    img = Image.new("RGB", size, (255, 255, 255))
    for x in range(100, size[0] - 100, 60):                                # some content to bound
        for y in range(100, size[1] - 100, 60):
            img.paste((20, 20, 120), (x, y, x + 30, y + 30))
    path = tmp_path / name
    img.save(path)
    return str(path)


def test_figure_camera_starts_whole_then_zooms_and_travels(tmp_path):
    cam = s4._Camera(_zoom_source(tmp_path, (2800, 900)), "figure")
    first = cam.view_rect(0, 5)
    later = [cam.view_rect(k, 5) for k in range(1, 5)]
    assert first[4] < later[0][4]                                          # zoomed in after the first window
    assert abs(later[-1][4] / first[4] - s4.ZOOM_MAX) < 1e-6
    xs = [(r[0] + r[2]) / 2 for r in later]
    assert xs == sorted(xs) and xs[-1] - xs[0] > 100                       # sweeps along the long side
    bx0, by0, bx1, by1 = cam.box
    for r in [first] + later:                                              # the view never leaves the content
        assert (r[0] >= bx0 - 1e-6 and r[2] <= bx1 + 1e-6) or r[2] - r[0] >= bx1 - bx0   # inside, or centred when larger
        assert (r[1] >= by0 - 1e-6 and r[3] <= by1 + 1e-6) or r[3] - r[1] >= by1 - by0


def test_page_camera_scrolls_top_to_bottom_at_a_fixed_scale(tmp_path):
    cam = s4._Camera(_zoom_source(tmp_path, (1900, 2500)), "page")
    views = [cam.view_rect(k, 6) for k in range(6)]
    assert len({round(v[4], 6) for v in views}) == 1
    ys = [v[1] for v in views]
    assert ys == sorted(ys) and ys[-1] > ys[0]
    assert abs(views[-1][3] - cam.box[3]) < 1.0                            # ends at the bottom of the content
    assert views[0][4] <= s4.PAGE_MAX_SCALE


def test_camera_render_fills_the_visual_area(tmp_path):
    for kind, size in (("figure", (2800, 900)), ("page", (1900, 2500))):
        cam = s4._Camera(_zoom_source(tmp_path, size, kind + ".png"), kind)
        for k in (0, 2, 4):
            assert cam.render(k, 5).size == (s4.VIS_W, s4.VIS_H)


def test_paper_figure_frames_change_per_window_and_zoom_can_be_disabled(tmp_path, monkeypatch):
    path = _zoom_source(tmp_path, (2800, 900))
    tl = _timeline(path, "figure")
    (tmp_path / "on").mkdir()
    s4.build_frames({"title": "T", "key_points": ["x"]}, tl, tmp_path / "on", "ru")
    shots = {_visual({"frames": [f]}).tobytes() for f in tl[0]["frames"]}
    assert len(tl[0]["frames"]) > 2 and len(shots) == len(tl[0]["frames"])  # a different view in every window

    monkeypatch.setattr(s4, "ZOOM_MAX", 1.0)
    tl_off = _timeline(path, "figure")
    (tmp_path / "off").mkdir()
    s4.build_frames({"title": "T", "key_points": ["x"]}, tl_off, tmp_path / "off", "ru")
    assert len({_visual({"frames": [f]}).tobytes() for f in tl_off[0]["frames"]}) == 1   # static, as before
