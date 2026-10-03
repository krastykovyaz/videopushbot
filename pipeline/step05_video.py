"""
Шаг 5: Сборка финального видео через ffmpeg.
Каждый кадр держится ровно столько, сколько длится реплика.
"""

import json
import logging
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("step05")


def build_video(frames_dir: Path, timeline: list[dict], job_dir: Path) -> Path:
    """
    Собирает MP4 из кадров + аудио.
    Возвращает путь к готовому видео.
    """
    audio_path = job_dir / "final_audio.mp3"
    output_path = job_dir / "output_video.mp4"

    if not audio_path.exists():
        raise FileNotFoundError(f"Аудио не найдено: {audio_path}")

    # Создать concat-файл для ffmpeg
    concat_path = job_dir / "concat.txt"
    _write_concat(timeline, frames_dir, concat_path)

    # A fixed 600s limit killed long renders on a small server (-preset slow at
    # 1080p); allow ~3x the audio length, never less than 10 minutes.
    total_sec = sum(e["duration_ms"] for e in timeline) / 1000.0
    _run_ffmpeg(concat_path, audio_path, output_path, timeout=max(600, int(total_sec * 3)))

    size_mb = output_path.stat().st_size / 1024 / 1024
    log.info(f"Видео готово: {size_mb:.1f} МБ → {output_path}")
    return output_path


def _write_concat(timeline: list[dict], frames_dir: Path, concat_path: Path):
    """Записать ffmpeg concat-файл с длительностью каждого кадра."""
    lines = []
    valid_entries = []

    previous = None
    for entry in timeline:
        frame = Path(entry.get("frame_path") or frames_dir / f"frame_{entry['seg_id']:04d}.png")
        if not frame.exists():
            # Skipping the frame (old behaviour) shifted every later slide earlier
            # than its audio. Hold the previous slide for this segment's duration.
            if previous is None:
                log.warning(f"Кадр не найден: {frame}, пропускаю (нет предыдущего кадра)")
                continue
            log.warning(f"Кадр не найден: {frame}, показываю предыдущий кадр")
            frame = previous
        previous = frame
        valid_entries.append((frame, entry))
    if not valid_entries:
        raise RuntimeError(f"Нет ни одного кадра в {frames_dir}")

    for i, (frame, entry) in enumerate(valid_entries):
        duration_sec = entry["duration_ms"] / 1000.0 + 0.3
        lines.append(f"file '{frame.resolve()}'")
        lines.append(f"duration {duration_sec:.3f}")

    # Последний кадр повторяем без duration (требование ffmpeg concat)
    if valid_entries:
        last_frame = valid_entries[-1][0]
        lines.append(f"file '{last_frame.resolve()}'")

    with open(concat_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    log.info(f"Concat файл: {len(valid_entries)} кадров → {concat_path}")


def _run_ffmpeg(concat_path: Path, audio_path: Path, output_path: Path, timeout: int = 600):
    """Запустить ffmpeg для сборки видео."""
    cmd = [
        "ffmpeg", "-y",
        # Входные кадры
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_path),
        # Аудио
        "-i", str(audio_path),
        # Видео — высокое качество для статичных слайдов
        "-c:v", "libx264",
        "-preset", "slow",
        "-crf", "18",
        "-tune", "stillimage",    # оптимизация кодека для статичного контента
        "-pix_fmt", "yuv420p",
        "-r", "24",              # 24fps — ffmpeg concat лучше работает с нормальным fps
        "-vf", (
            "scale=1920:1080:force_original_aspect_ratio=decrease,"
            "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=0f0f14,"
            "format=yuv420p"
        ),
        # Аудио — высокое качество
        "-c:a", "aac",
        "-b:a", "192k",
        "-ar", "44100",          # 44.1kHz (не дефолтные 24kHz Edge TTS)
        "-ac", "2",              # стерео
        "-shortest",
        "-movflags", "+faststart",
        str(output_path),
    ]

    log.info(f"ffmpeg: {' '.join(cmd[:8])}...")
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        log.error(f"ffmpeg stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"ffmpeg завершился с кодом {result.returncode}")

    log.info("ffmpeg завершён успешно")