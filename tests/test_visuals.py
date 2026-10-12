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
