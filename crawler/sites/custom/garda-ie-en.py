# -*- coding: utf-8 -*-
"""An Garda Síochána – Annual Reports crawler.

Starting page: https://www.garda.ie/en/information-centre/annual-reports/
(redirects to the canonical annual-reports listing page)
The listing page contains direct PDF file links inside a file-list container.
Abstract text is extracted from the PDFs themselves via pdftotext / pypdf fallback.
"""

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, quote

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_START_URL = "https://www.garda.ie/en/information-centre/annual-reports/"
_BASE = "https://www.garda.ie"
_PUBLISHER = "An Garda Síochána"
_CATEGORY = "Annual Report"

_UA = BaseCrawler.USER_AGENT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3, binary: bool = True):
    """Fetch *url* via curl (TLS workaround).  Returns bytes/str or None."""
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-skL",
                    "--max-time", "45",
                    "-A", _UA,
                    url,
                ],
                capture_output=True,
                timeout=50,
            )
            if result.stdout:
                return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[garda-ie-en] curl error ({url}): {exc}")
        if attempt < retries - 1:
            wait = delays[attempt]
            print(f"[garda-ie-en] retrying {url} in {wait}s …")
            time.sleep(wait)
    return None


def _make_soup(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _pdf_text(pdf_bytes: bytes) -> str:
    """Extract text from PDF bytes: pdftotext → pypdf fallback."""
    # pdftotext via temp file
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tf_path = tf.name
        result = subprocess.run(
            ["pdftotext", "-layout", tf_path, "-"],
            capture_output=True,
            timeout=30,
        )
        try:
            os.unlink(tf_path)
        except OSError:
            pass
        text = result.stdout.decode("utf-8", errors="replace").strip()
        if text:
            return text
    except Exception:
        pass

    # pypdf fallback
    try:
        import pypdf  # noqa: PLC0415
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        parts = []
        for page in reader.pages[:15]:
            t = page.extract_text() or ""
            if t:
                parts.append(t)
        return "\n".join(parts).strip()
    except Exception:
        pass

    return ""


def _parse_date(raw: str) -> str:
    """'DD/MM/YYYY' → 'YYYY-MM-DD'; pass through anything else unchanged."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw.strip()


def _parse_page(html: str, page_url: str):
    """Parse one Garda publications page.

    Returns:
        subpage_urls  – list of sub-page URLs discovered (landing-document links)
        pdf_items     – list of dicts describing each PDF link found
    """
    soup = _make_soup(html)
    if not soup:
        return [], []

    subpage_urls = []
    pdf_items = []

    # Sub-page links inside landing-document containers
    for div in soup.find_all("div", class_="landing-document"):
        a = div.find("a")
        if not a:
            continue
        href = a.get("href", "")
        if href and not href.lower().endswith(".pdf"):
            subpage_urls.append(urljoin(_BASE, href))

    # PDF entries inside file-list containers
    for file_list_div in soup.find_all("div", class_="file-list"):
        for li in file_list_div.find_all("li"):
            a = li.find("a")
            if not a:
                continue
            href = a.get("href", "")
            if not href.lower().endswith(".pdf"):
                continue

            title_attr = (a.get("title") or "").strip()
            name_tag = li.find("span", class_="file-name")
            display_name = name_tag.get_text(strip=True) if name_tag else ""
            title = title_attr or display_name

            date_tag = li.find("span", class_="file-date")
            date_raw = date_tag.get_text(strip=True) if date_tag else ""

            size_tag = li.find("span", class_="file-size")
            file_size = size_tag.get_text(strip=True) if size_tag else ""

            ext_tag = li.find("span", class_="file-extension")
            file_ext = ext_tag.get_text(strip=True) if ext_tag else "pdf"

            pdf_url = urljoin(_BASE, quote(href, safe="/:?=&#@!$'()*+,;"))
            filename = Path(href.split("?")[0]).name
            slug = Path(href.split("?")[0]).stem

            pdf_items.append({
                "title": title,
                "date_raw": date_raw,
                "file_size": file_size,
                "file_ext": file_ext,
                "pdf_url": pdf_url,
                "filename": filename,
                "slug": slug,
                "page_url": page_url,
            })

    return subpage_urls, pdf_items


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class GardaIeEnCrawler(BaseCrawler):
    """Crawler for An Garda Síochána Annual Reports."""

    site_id = "garda-ie-en"
    site_name = "Custom: garda-ie-en"
    base_url = "https://www.garda.ie"

    def crawl(self, limit=None):  # noqa: C901
        start_wall = time.time()
        MAX_WALL = 25 * 60  # 25-minute hard budget
        PAGE_CAP = 200      # safety cap on pages visited

        saved = 0
        seen_pdf_urls: set = set()
        visited_pages: set = set()
        page_queue = [_START_URL]
        pages_visited = 0

        while page_queue:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_wall > MAX_WALL:
                print("[garda-ie-en] 25-minute wall-clock budget reached – stopping.")
                break
            if pages_visited >= PAGE_CAP:
                print(f"[garda-ie-en] Safety cap of {PAGE_CAP} pages reached – stopping discovery.")
                break

            page_url = page_queue.pop(0)
            if page_url in visited_pages:
                continue
            visited_pages.add(page_url)
            pages_visited += 1

            if pages_visited % 10 == 0:
                limit_label = str(limit) if limit is not None else "∞"
                print(f"[garda-ie-en] page {pages_visited}: saved {saved}/{limit_label}")

            print(f"[garda-ie-en] Fetching page: {page_url}")
            raw = _curl_get(page_url, binary=False)
            if not raw:
                print(f"[garda-ie-en] Failed to fetch page: {page_url} – skipping.")
                continue

            subpages, pdf_items = _parse_page(raw, page_url)

            # Queue unvisited sub-pages (append to end — breadth-first)
            for sp in subpages:
                if sp not in visited_pages and sp not in page_queue:
                    page_queue.append(sp)

            # Process PDF items found on this page
            for item in pdf_items:
                if limit is not None and saved >= limit:
                    break

                pdf_url = item["pdf_url"]
                if pdf_url in seen_pdf_urls:
                    print(f"[garda-ie-en] Duplicate PDF skipped: {pdf_url}")
                    continue
                seen_pdf_urls.add(pdf_url)

                try:
                    title = item["title"].strip()
                    date_raw = item["date_raw"]
                    filename = item["filename"]
                    slug = item["slug"]
                    file_size = item.get("file_size", "")
                    file_ext = item.get("file_ext", "pdf")
                    listed_date = _parse_date(date_raw)
                    source_page = item["page_url"]

                    print(f"[garda-ie-en] [{saved + 1}] Fetching PDF: {filename}")

                    pdf_bytes = _curl_get(pdf_url, binary=True)
                    if not pdf_bytes:
                        print(f"[garda-ie-en] Failed to fetch PDF: {pdf_url} – skipping.")
                        continue

                    abstract = _pdf_text(pdf_bytes)

                    if len(abstract) < 50:
                        print(
                            f"[garda-ie-en] Abstract too short "
                            f"({len(abstract)} chars) for {title!r} – skipping."
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": listed_date,
                        "listed_date": listed_date,
                        "url": source_page,
                        "pdf_url": pdf_url,
                        "publisher": _PUBLISHER,
                        "authors": "",
                        "keywords": "",
                        "category": _CATEGORY,
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "original_filename": filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_raw,
                                "originalFilename": filename,
                                "file_extension": file_ext,
                                "file_size": file_size,
                                "category": _CATEGORY,
                                "source_page": source_page,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_label = str(limit) if limit is not None else "∞"
                    print(f"[garda-ie-en] Saved {saved}/{limit_label}: {title[:70]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[garda-ie-en] Item failed ({item.get('filename', '?')}): {exc}")
                    continue

        print(f"[garda-ie-en] Done. Total saved: {saved}")
        return saved
