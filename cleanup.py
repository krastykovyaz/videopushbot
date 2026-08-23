"""
Disk housekeeping for a small server: the pipeline produces a full PDF +
extracted images + TTS audio + rendered frames + final MP4 per job (often
several hundred MB to a few GB each), and log files grow unbounded otherwise.

Deletes anything older than RETENTION_DAYS under workspace/, uploaded_videos/,
and videos_to_upload/ (structure-agnostic: just sweeps files by mtime, so it
doesn't care whether a job lives at workspace/<user_id>/<job_id>/ or
workspace/url_batch/<job_id>/), truncates any *.log file over MAX_LOG_MB,
and clears stale __pycache__ directories.

Meant to run daily via cron/systemd timer — see deploy/notebooklm-cleanup.*
Safe to run by hand any time: `python3 cleanup.py` (add --dry-run to preview).
"""

import argparse
import logging
import os
import shutil
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("cleanup")

BASE_DIR = Path(__file__).parent
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS", "7"))
MAX_LOG_MB = int(os.getenv("MAX_LOG_MB", "20"))

MEDIA_DIRS = ["workspace", "uploaded_videos", "videos_to_upload"]


def _clean_stale_files(directory: Path, retention_seconds: float, dry_run: bool) -> tuple[int, int]:
    """Deletes files older than retention_seconds under directory (recursive)."""
    if not directory.exists():
        return 0, 0
    removed_files = 0
    removed_bytes = 0
    now = time.time()
    for f in directory.rglob("*"):
        if not f.is_file():
            continue
        try:
            age = now - f.stat().st_mtime
            if age <= retention_seconds:
                continue
            size = f.stat().st_size
            if not dry_run:
                f.unlink()
            removed_files += 1
            removed_bytes += size
        except FileNotFoundError:
            pass
    return removed_files, removed_bytes


def _prune_empty_dirs(directory: Path, dry_run: bool) -> int:
    if not directory.exists():
        return 0
    removed = 0
    # Deepest first so nested empty dirs collapse correctly in one pass.
    for d in sorted((p for p in directory.rglob("*") if p.is_dir()), key=lambda p: -len(p.parts)):
        try:
            if not any(d.iterdir()):
                if not dry_run:
                    d.rmdir()
                removed += 1
        except FileNotFoundError:
            pass
    return removed


def clean_media(dry_run: bool):
    retention_seconds = RETENTION_DAYS * 86400
    for name in MEDIA_DIRS:
        d = BASE_DIR / name
        files, freed = _clean_stale_files(d, retention_seconds, dry_run)
        if files:
            verb = "would remove" if dry_run else "removed"
            log.info(f"{name}/: {verb} {files} files older than {RETENTION_DAYS}d "
                      f"({freed / 1024**3:.2f} GB)")
        pruned = _prune_empty_dirs(d, dry_run)
        if pruned:
            verb = "would prune" if dry_run else "pruned"
            log.info(f"{name}/: {verb} {pruned} empty directories")


def clean_logs(dry_run: bool):
    max_bytes = MAX_LOG_MB * 1024 * 1024
    for f in BASE_DIR.glob("*.log"):
        try:
            size = f.stat().st_size
        except FileNotFoundError:
            continue
        if size <= max_bytes:
            continue
        verb = "would truncate" if dry_run else "truncated"
        if not dry_run:
            # Truncate in place (safe even if a running process has it open —
            # it keeps writing at its current offset into the same inode).
            f.write_text("")
        log.info(f"{f.name}: {verb} ({size / 1024**2:.1f} MB over {MAX_LOG_MB} MB cap)")


def clean_pycache(dry_run: bool):
    removed = 0
    for d in BASE_DIR.rglob("__pycache__"):
        if d.is_dir():
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
            removed += 1
    if removed:
        verb = "would remove" if dry_run else "removed"
        log.info(f"__pycache__: {verb} {removed} directories")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would be removed, change nothing")
    args = parser.parse_args()

    log.info(f"Housekeeping started (retention={RETENTION_DAYS}d, log cap={MAX_LOG_MB}MB, dry_run={args.dry_run})")
    clean_media(args.dry_run)
    clean_logs(args.dry_run)
    clean_pycache(args.dry_run)
    log.info("Housekeeping done")


if __name__ == "__main__":
    main()
