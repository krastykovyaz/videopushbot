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
    candidate_imgs = scope.find_all("img")
    if not candidate_imgs and scope is not soup:
        # Some sites render real content images outside <article>/<main>
        # (e.g. a separate gallery/media container) — fall back to the
        # whole page rather than silently returning zero images.
        candidate_imgs = soup.find_all("img")

    images = []
    seen = set()
    idx = 0

    for img in candidate_imgs:
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

        downloaded = _download_image(real_url, img_dir, idx)
        if downloaded:
            images.append({"path": downloaded, "caption": alt, "bbox_y": float(idx)})
            idx += 1

    if not images:
        # Some sites (e.g. Next.js apps like za rulem) render the actual
        # article image client-side rather than as a plain <img> tag, so the
        # scans above find nothing but site-chrome icons. The og:image /
        # twitter:image meta tags are a near-universal fallback that still
        # point at the real article hero image.
        meta = soup.find("meta", property="og:image") or soup.find("meta", attrs={"name": "twitter:image"})
        if meta and meta.get("content"):
            real_url = _resolve_image_url(meta["content"].strip(), page_url)
            if real_url:
                title_meta = soup.find("meta", property="og:title")
                caption = (title_meta.get("content").strip() if title_meta and title_meta.get("content") else "")
                downloaded = _download_image(real_url, img_dir, 0)
                if downloaded:
                    images.append({"path": downloaded, "caption": caption, "bbox_y": 0.0})

    return images


def _download_image(url: str, img_dir: Path, idx: int) -> str | None:
    try:
        r = requests.get(url, headers=_HEADERS, timeout=20)
        r.raise_for_status()
        if "image" not in r.headers.get("Content-Type", ""):
            return None
        if len(r.content) < _MIN_IMAGE_BYTES:
            return None
        ext = ".jpg" if "jpeg" in r.headers.get("Content-Type", "") else ".png"
        img_path = img_dir / f"web_{idx:02d}{ext}"
        img_path.write_bytes(r.content)
        return str(img_path)
    except Exception as e:
        log.warning(f"webpage_extract: failed to download image {url}: {e}")
        return None


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


def find_article_links(listing_url: str, limit: int = 5, path_regex: "re.Pattern" = None) -> list[str]:
    """
    Scans a listing page for article links matching `path_regex` (CoinDesk's
    dated-path pattern by default), most recent first, deduplicated, capped
    at `limit`.
    """
    pattern = path_regex or _ARTICLE_PATH_RE
    resp = requests.get(listing_url, headers=_HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    base = f"{urlparse(listing_url).scheme}://{urlparse(listing_url).netloc}"
    seen = set()
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        path = urlparse(href).path if href.startswith("http") else href
        if pattern.match(path):
            full_url = href if href.startswith("http") else base + path
            if full_url not in seen:
                seen.add(full_url)
                links.append(full_url)
        if len(links) >= limit:
            break

    return links


# ── Russian automotive news sources ("one story a day" feature) ─────────────
# Each source's listing page + the regex matching a real article path there
# (vs. section/tag/pagination links, which share the same nav chrome).
RU_AUTO_SOURCES = {
    "zr.ru": {
        "listing_url": "https://www.zr.ru/news/",
        "path_regex": re.compile(r"^/content/news/\d+-[a-z0-9-]+/?$"),
    },
    "kolesa.ru": {
        "listing_url": "https://www.kolesa.ru/news/",
        "path_regex": re.compile(r"^/(news|article)/[a-z0-9-]+/?$"),
    },
    "autonews.ru": {
        "listing_url": "https://www.autonews.ru/news",
        "path_regex": re.compile(r"^/news/[0-9a-f]{24}$"),
    },
}


def find_ru_auto_links(limit_per_source: int = 5) -> list[str]:
    """Collects candidate article URLs across all RU_AUTO_SOURCES listing
    pages. A failure on one source is logged and skipped, not fatal."""
    links = []
    for name, src in RU_AUTO_SOURCES.items():
        try:
            links.extend(find_article_links(src["listing_url"], limit_per_source, src["path_regex"]))
        except Exception as e:
            log.warning(f"find_ru_auto_links: {name} listing fetch failed: {e}")
    return links


# ── Quarterly all-countries import digest ("итоги квартала") ────────────────
# No site publishes a stable listing page dedicated to these — they're rare
# (event-driven, roughly quarterly) articles mixed into each site's regular
# news feed. Reuses RU_AUTO_SOURCES' listings plus autoreview.ru, scanned
# deeper than the daily check, then filtered by title keywords.
QUARTERLY_DIGEST_SOURCES = dict(RU_AUTO_SOURCES, **{
    "autoreview.ru": {
        "listing_url": "https://autoreview.ru/news",
        "path_regex": re.compile(r"^/news/[a-z0-9-]+$"),
    },
})

_DIGEST_KEYWORDS_RE = re.compile(
    r"(импорт|ввоз|поставк).{0,40}(итог|квартал|полугод)|"
    r"(итог|квартал|полугод).{0,40}(импорт|ввоз|поставк)",
    re.IGNORECASE,
)


def find_quarterly_digest_candidates(limit_per_source: int = 20) -> list[dict]:
    """Scans all QUARTERLY_DIGEST_SOURCES listings for recent article links,
    filtered to those whose title reads like an all-countries import
    quarter/half-year wrap-up. Returns [{"url", "title"}, ...]."""
    found = []
    for name, src in QUARTERLY_DIGEST_SOURCES.items():
        try:
            candidates = find_article_links(src["listing_url"], limit_per_source, src["path_regex"])
        except Exception as e:
            log.warning(f"find_quarterly_digest_candidates: {name} listing fetch failed: {e}")
            continue
        for url in candidates:
            try:
                title = extract_page_title(url)
            except Exception as e:
                log.warning(f"find_quarterly_digest_candidates: title fetch failed for {url}: {e}")
                continue
            if _DIGEST_KEYWORDS_RE.search(title):
                found.append({"url": url, "title": title})
    return found


def extract_page_title(url: str) -> str:
    """Cheap og:title/<title> fetch, for comparing candidates before
    committing to a full extract_webpage() call on the chosen one."""
    resp = requests.get(url, headers=_HEADERS, timeout=20)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    meta = soup.find("meta", property="og:title")
    if meta and meta.get("content"):
        return meta["content"].strip()
    if soup.title and soup.title.string:
        return soup.title.string.strip()
    return url
