#!/usr/bin/env python3
"""
Unified pipeline: takes a list of URLs, keeps only real downloadable PDFs,
generates narrated videos via the existing PDF->video pipeline
(pipeline/step01-06, via queue_worker.run_pipeline), and publishes them to
the right YouTube/VK channels automatically — no manual Telegram relay.

Routing: automotive/car content -> RU only (VK + YouTube-RU).
         everything else        -> both (YouTube-EN and YouTube-RU + VK).

Usage:
    python publish_from_urls.py --urls-file urls.txt [--dry-run]
    python publish_from_urls.py --urls URL1 URL2 ... [--dry-run]
"""

import argparse
import asyncio
import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

import config_en
import config_ru2
from common.gemini_client import GeminiContentGenerator
from common.metadata import load_script_title_and_points
from common.pdf_source import download_pdf, is_suitable_pdf_url
from common.vk_uploader import VKUploader
from common.youtube_uploader import YouTubeUploader
from pipeline.step01_extract import extract_pdf
from queue_worker import Job, run_pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("publish_from_urls.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("publish_from_urls")

AUTOMOTIVE_CATEGORY = "Auto Detail"


async def cli_progress(chat_id, text):
    log.info(text)


async def process_url(index: int, url: str, workspace: Path,
                       gemini_ru, gemini_en, yt_ru, yt_en, vk, dry_run: bool) -> dict:
    suitable, reason = is_suitable_pdf_url(url)
    if not suitable:
        log.warning(f"[SKIP] {url} — {reason}")
        return {"url": url, "status": "skipped", "reason": reason}

    job_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{index:02d}"
    base_dir = workspace / job_id
    base_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = base_dir / "source.pdf"

    try:
        download_pdf(url, pdf_path)
    except Exception as e:
        log.error(f"[FAIL] {url} — download failed: {e}")
        return {"url": url, "status": "failed", "reason": f"download failed: {e}"}

    blocks = await asyncio.to_thread(extract_pdf, pdf_path, base_dir / "sample")
    sample_text = "\n\n".join(b["text"] for b in blocks[:5])

    category = gemini_ru.classify_topic(
        pdf_path.stem, sample_text, list(config_ru2.CONFIG["playlists"].keys()))
    if not category:
        category = "Other"
        log.warning(f"{url}: topic classification failed, no playlist match — proceeding without a playlist")

    lang_modes = ["ru"] if category == AUTOMOTIVE_CATEGORY else ["ru", "en"]
    log.info(f"{url}: category='{category}' -> lang_modes={lang_modes}")

    result = {"url": url, "status": "processed", "category": category, "langs": {}}

    for lang in lang_modes:
        lang_dir = base_dir / lang
        lang_dir.mkdir(exist_ok=True)
        lang_pdf = lang_dir / "input.pdf"
        shutil.copy2(pdf_path, lang_pdf)

        job = Job(
            job_id=f"{job_id}_{lang}",
            user_id=0,
            chat_id=0,
            pdf_path=lang_pdf,
            job_dir=lang_dir,
            lang=lang,
            progress_fn=cli_progress,
        )
        video_path, thumb_path = await run_pipeline(job)

        title, key_points = load_script_title_and_points(lang_dir)
        gemini = gemini_ru if lang == "ru" else gemini_en
        description = gemini.generate_description(title, key_points, lang=lang)
        description += f"\n\nSource: {url}"

        if lang == "ru":
            playlist_id = config_ru2.CONFIG["playlists"].get(category)
            vk_owner_id = config_ru2.CONFIG["vk"]["channels"].get(category)
        else:
            playlist_id = config_en.CONFIG["playlists"].get(category)
            vk_owner_id = None

        lang_result = {
            "title": title,
            "video_path": str(video_path),
            "thumbnail_path": str(thumb_path),
            "playlist_id": playlist_id,
            "vk_owner_id": vk_owner_id,
        }

        if dry_run:
            log.info(f"DRY RUN — would upload [{lang}] '{title}' -> "
                      f"youtube_playlist={playlist_id}, vk_owner_id={vk_owner_id}")
        else:
            yt = yt_ru if lang == "ru" else yt_en
            if yt:
                lang_result["youtube"] = yt.upload_video(
                    str(video_path), title, description,
                    thumbnail_path=str(thumb_path), playlist_id=playlist_id)
            if lang == "ru" and vk and vk_owner_id:
                lang_result["vk"] = vk.upload_video(
                    str(video_path), title, description,
                    owner_id=vk_owner_id, thumbnail_path=str(thumb_path))

        result["langs"][lang] = lang_result

    return result


def init_uploaders(dry_run: bool):
    if dry_run:
        return None, None, None

    yt_ru = yt_en = vk = None

    if config_ru2.CONFIG["youtube"]["auto_upload"] and os.path.exists(config_ru2.CONFIG["youtube"]["client_secrets_file"]):
        yt_ru = YouTubeUploader(
            config_ru2.CONFIG["youtube"]["client_secrets_file"],
            token_file="youtube_token_ru.pickle",
            oauth_ports=(8080, 8081),
            category_id=config_ru2.CONFIG["youtube"]["category_id"],
            privacy_status=config_ru2.CONFIG["youtube"]["privacy_status"],
        )

    if config_en.CONFIG["youtube"]["auto_upload"] and os.path.exists(config_en.CONFIG["youtube"]["client_secrets_file"]):
        yt_en = YouTubeUploader(
            config_en.CONFIG["youtube"]["client_secrets_file"],
            token_file="youtube_token_en.pickle",
            oauth_ports=(8080, 8081),
            category_id=config_en.CONFIG["youtube"]["category_id"],
            privacy_status=config_en.CONFIG["youtube"]["privacy_status"],
        )

    if config_ru2.CONFIG.get("vk", {}).get("access_token"):
        vk = VKUploader(config_ru2.CONFIG["vk"]["access_token"])

    return yt_ru, yt_en, vk


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--urls-file", type=Path, help="text file, one URL per line (# comments allowed)")
    parser.add_argument("--urls", nargs="*", default=[], help="URLs given directly on the command line")
    parser.add_argument("--dry-run", action="store_true",
                         help="generate everything but skip YouTube/VK publish calls")
    parser.add_argument("--workspace", type=Path, default=Path("./workspace/url_batch"))
    args = parser.parse_args()

    urls = list(args.urls)
    if args.urls_file:
        for line in args.urls_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)

    if not urls:
        parser.error("provide --urls-file and/or --urls")

    args.workspace.mkdir(parents=True, exist_ok=True)

    gemini_ru = GeminiContentGenerator(config_ru2.CONFIG["gemini"]["api_key"], config_ru2.CONFIG["gemini"]["model"])
    gemini_en = GeminiContentGenerator(config_en.CONFIG["gemini"]["api_key"], config_en.CONFIG["gemini"]["model"])
    yt_ru, yt_en, vk = init_uploaders(args.dry_run)

    results = []
    for i, url in enumerate(urls, 1):
        log.info(f"=== [{i}/{len(urls)}] {url} ===")
        try:
            result = await process_url(i, url, args.workspace, gemini_ru, gemini_en, yt_ru, yt_en, vk, args.dry_run)
        except Exception as e:
            log.exception(f"Unhandled error processing {url}")
            result = {"url": url, "status": "failed", "reason": str(e)}
        results.append(result)

    log.info("=== SUMMARY ===")
    for r in results:
        log.info(f"{r['url']}: {r['status']}" + (f" ({r.get('reason')})" if r.get("reason") else ""))

    summary_path = args.workspace / "run_summary.json"
    summary_path.write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str))
    log.info(f"Summary written to {summary_path}")


if __name__ == "__main__":
    asyncio.run(main())
