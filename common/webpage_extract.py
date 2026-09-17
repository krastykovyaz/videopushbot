"""
Extracts article text + images from a webpage, shaped to match
pipeline/step01_extract.py's extract_pdf() output format, so the rest of
the pipeline (script generation, TTS, frames, thumbnail) works unchanged
regardless of whether the source was a PDF or a web article.

The extracted text is only ever used as grounding material for the existing
Gemini script-generation step, which already writes an original two-host
discussion rather than reading the source verbatim (same pattern already
used for PDF sources) — never reproduced as-is.
"""

import json
import logging
import re
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse

import requests
import trafilatura
from bs4 import BeautifulSoup

log = logging.getLogger("webpage_extract")

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}
_MAX_IMAGES = 6
_MIN_IMAGE_BYTES = 5000  # skip tiny icons/placeholders


def extract_webpage(url: str, job_dir: Path) -> list[dict]:
    """
    Returns [{"page": 0, "text": str, "images": [{"path", "caption", "bbox_y"}]}]
    — same shape as pipeline.step01_extract.extract_pdf(), saved to the same
    job_dir/extracted/text_blocks.json path.
    """
    out_dir = job_dir / "extracted"
    img_dir = out_dir / "images"
    img_dir.mkdir(parents=True, exist_ok=True)

    resp = requests.get(url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    html = resp.text

    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    if not text.strip():
        raise ValueError(f"No article text found at {url} (trafilatura extraction empty)")

    images = _extract_images(html, url, img_dir)
    pages = [{"page": 0, "text": text, "images": images}]

    with open(out_dir / "text_blocks.json", "w", encoding="utf-8") as f:
        json.dump(pages, f, ensure_ascii=False, indent=2)

    log.info(f"webpage_extract: {len(text)} chars text, {len(images)} images from {url}")
    return pages


def _extract_images(html: str, page_url: str, img_dir: Path) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    # Scope to the article body specifically — the full page also carries
    # promotional banners (event ads, section teasers) with plausible-looking
    # alt text that aren't part of the actual article content.
    scope = soup.find("article") or soup.find("main") or soup
    images = []
    seen = set()
    idx = 0

    for img in scope.find_all("img"):
        if idx >= _MAX_IMAGES:
            break
        alt = (img.get("alt") or "").strip()
        src = img.get("src") or img.get("data-src") or ""
        if not src or not alt:  # unlabeled images are almost always decorative chrome
            continue

        real_url = _resolve_image_url(src, page_url)
        if not real_url or real_url in seen:
            continue
        seen.add(real_url)

        try:
            r = requests.get(real_url, headers=_HEADERS, timeout=20)
            r.raise_for_status()
            if "image" not in r.headers.get("Content-Type", ""):
                continue
            if len(r.content) < _MIN_IMAGE_BYTES:
                continue
            ext = ".jpg" if "jpeg" in r.headers.get("Content-Type", "") else ".png"
            img_path = img_dir / f"web_{idx:02d}{ext}"
            img_path.write_bytes(r.content)
            images.append({"path": str(img_path), "caption": alt, "bbox_y": float(idx)})
            idx += 1
        except Exception as e:
            log.warning(f"webpage_extract: failed to download image {real_url}: {e}")

    return images


def _resolve_image_url(src: str, page_url: str) -> str | None:
    """Handles plain URLs, protocol-relative URLs, and Next.js's image proxy
    (/_next/image?url=<encoded-original>&...), which is what CoinDesk uses."""
    if src.startswith("//"):
        return "https:" + src
    if src.startswith("/"):
        absolute = urljoin(page_url, src)
        parsed = urlparse(absolute)
        if "/_next/image" in parsed.path:
            qs = parse_qs(parsed.query)
            if "url" in qs:
                return qs["url"][0]
        return absolute
    return src


# Matches CoinDesk-style dated article paths, e.g. /policy/2026/09/13/some-slug
_ARTICLE_PATH_RE = re.compile(r"^/[a-z0-9-]+/\d{4}/\d{2}/\d{2}/[a-z0-9-]+/?$")


def find_article_links(listing_url: str, limit: int = 5) -> list[str]:
    """
    Scans a CoinDesk newsletter listing page for article links, most recent
    first, deduplicated, capped at `limit`.
    """
    resp = requests.get(listing_url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    base = f"{urlparse(listing_url).scheme}://{urlparse(listing_url).netloc}"
    seen = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = urlparse(href).path if href.startswith("http") else href
        if _ARTICLE_PATH_RE.match(path):
            full_url = href if href.startswith("http") else base + path
            if full_url not in seen:
                seen.add(full_url)
                links.append(full_url)
        if len(links) >= limit:
            break

    return links
