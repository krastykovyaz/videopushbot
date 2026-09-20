"""
Fetch a PDF from a URL, with a suitability filter that only accepts real,
directly-downloadable PDFs (not homepages/article pages).
"""

import logging
from pathlib import Path
from urllib.parse import urlparse

import requests

log = logging.getLogger("pdf_source")

_PDF_MAGIC = b"%PDF-"

# Some servers reject requests carrying the default python-requests User-Agent
# (seen against am.jpmorgan.com, which resets the connection outright).
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


def is_suitable_pdf_url(url: str, timeout: int = 10) -> tuple[bool, str]:
    """
    Returns (suitable, reason). A URL is suitable only if it plausibly points
    directly at a PDF file — a homepage or article page is not suitable, even
    if it *links to* PDFs elsewhere on the site.
    """
    path = urlparse(url).path
    looks_like_pdf_path = path.lower().endswith(".pdf")

    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True, headers=_HEADERS)
        content_type = resp.headers.get("Content-Type", "").lower()

        if resp.status_code in (405, 403) or not content_type:
            # Some servers reject HEAD; fall back to a small ranged GET.
            resp = requests.get(url, timeout=timeout, allow_redirects=True,
                                 headers={**_HEADERS, "Range": "bytes=0-2048"}, stream=True)
            content_type = resp.headers.get("Content-Type", "").lower()
            chunk = next(resp.iter_content(chunk_size=2048), b"")
            resp.close()
            if chunk.startswith(_PDF_MAGIC):
                return True, "magic bytes match application/pdf"

        if "application/pdf" in content_type:
            return True, "Content-Type: application/pdf"

        if looks_like_pdf_path and resp.status_code < 400:
            return True, "URL path ends in .pdf"

        return False, f"not a PDF (Content-Type: {content_type or 'unknown'}, status {resp.status_code})"

    except requests.RequestException as e:
        if looks_like_pdf_path:
            return True, f"URL path ends in .pdf (request check failed: {e})"
        return False, f"request failed: {e}"


def get_pdf_change_marker(url: str, timeout: int = 15) -> str | None:
    """
    Cheap way to detect whether a PDF at a stable URL (same URL, content
    replaced in place periodically — e.g. JPMorgan's "latest weekly brief"
    link) has actually changed, without downloading the whole file. Returns
    an opaque marker to compare against a previously stored one, or None if
    the server exposes neither ETag nor Last-Modified (caller should treat
    that as "can't tell, skip this check").
    """
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True, headers=_HEADERS)
        etag = resp.headers.get("ETag", "")
        last_modified = resp.headers.get("Last-Modified", "")
        if not etag and not last_modified:
            return None
        return f"{etag}|{last_modified}"
    except requests.RequestException as e:
        log.warning(f"get_pdf_change_marker: HEAD {url} failed: {e}")
        return None


def download_pdf(url: str, dest_path: Path, max_bytes: int = 2 * 1024 * 1024 * 1024,
                  timeout: int = 120) -> Path:
    """
    Streams the URL to dest_path, enforcing a size cap and verifying the
    downloaded file actually starts with the PDF magic bytes.
    """
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    downloaded = 0
    with requests.get(url, timeout=timeout, stream=True, headers=_HEADERS) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=1024 * 1024):
                downloaded += len(chunk)
                if downloaded > max_bytes:
                    dest_path.unlink(missing_ok=True)
                    raise ValueError(f"{url}: exceeded max size {max_bytes} bytes")
                f.write(chunk)

    with open(dest_path, "rb") as f:
        head = f.read(len(_PDF_MAGIC))
    if head != _PDF_MAGIC:
        dest_path.unlink(missing_ok=True)
        raise ValueError(f"{url}: downloaded file is not a valid PDF (bad magic bytes)")

    log.info(f"Downloaded {url} -> {dest_path} ({downloaded} bytes)")
    return dest_path
