# -*- coding: utf-8 -*-
"""
Crawler for Skills Development Scotland — Publications.

Starting URL:
    https://www.skillsdevelopmentscotland.co.uk/publications-statistics/publications

API endpoint (discovered via JS bundle analysis):
    POST https://www.skillsdevelopmentscotland.co.uk/api/publications/search
    Body: {"page": N, "pageSize": 100}
    Response: {"page": N, "pageSize": 100, "totalItems": 1956,
               "publications": [{title, topic, publishedDate, linkUrl,
                                  isExternalLink, fileType, fileSize, description}, ...]}
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "skillsdevelopmentscotland-co-uk-publications-statist"
_BASE_URL = "https://www.skillsdevelopmentscotland.co.uk"
_SEARCH_API = f"{_BASE_URL}/api/publications/search"
_PAGE_SIZE = 100
_SAFETY_CAP = 200
_RATE_SLEEP = 1.0
_ABSTRACT_MIN = 100
_MAX_SECONDS = 25 * 60

_MONTH_MAP = {
    "January": "01", "February": "02", "March": "03", "April": "04",
    "May": "05", "June": "06", "July": "07", "August": "08",
    "September": "09", "October": "10", "November": "11", "December": "12",
}


def _curl_post(url: str, payload: dict, *, timeout: int = 30, retries: int = 3):
    """POST JSON payload via curl; return parsed dict or None on failure."""
    data = json.dumps(payload)
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(timeout),
        "-X", "POST",
        "-H", "Content-Type: application/json",
        "-H", "Accept: application/json",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-d", data,
        url,
    ]
    for attempt in range(retries):
        if attempt > 0:
            wait = 1 * (3 ** (attempt - 1))   # 1s, 3s, 9s
            time.sleep(wait)
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=timeout + 5
            )
            raw = result.stdout.decode("utf-8", errors="replace").strip()
            if raw:
                return json.loads(raw)
            print(
                f"[{_SITE_ID}] empty response from {url}, "
                f"attempt {attempt + 1}/{retries}"
            )
        except json.JSONDecodeError as exc:
            print(f"[{_SITE_ID}] JSON decode error: {exc}, attempt {attempt + 1}/{retries}")
        except Exception as exc:
            print(
                f"[{_SITE_ID}] curl error: {exc}, "
                f"attempt {attempt + 1}/{retries}"
            )
    return None


def _parse_date(raw: str) -> str:
    """Convert '01 May 2026' to 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip()
    parts = raw.split()
    if len(parts) == 3:
        day, month, year = parts
        m = _MONTH_MAP.get(month)
        if m:
            return f"{year}-{m}-{day.zfill(2)}"
    # ISO fallback
    match = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    if match:
        return match.group(1)
    return ""


def _extract_media_id(link_url: str) -> str:
    """Extract Umbraco media GUID from /media/<id>/filename.pdf."""
    if not link_url:
        return ""
    m = re.search(r"/media/([^/]+)/", link_url)
    return m.group(1) if m else ""


def _extract_filename(link_url: str) -> str:
    """Return the last path segment of a URL (filename)."""
    if not link_url:
        return ""
    return link_url.rstrip("/").split("/")[-1]


class SkillsDevelopmentScotlandPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: skillsdevelopmentscotland-co-uk-publications-statist"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Paginate /api/publications/search and save items with long enough abstracts."""
        saved = 0
        seen_urls: set = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()

        page = 1
        while True:
            # ---- safety guards ----
            if time.time() - start_time > _MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute budget reached, stopping cleanly")
                break
            if page > _SAFETY_CAP:
                print(f"[{_SITE_ID}] safety cap of {_SAFETY_CAP} pages reached, stopping")
                break
            if saved >= limit_or_inf:
                break

            # ---- progress logging every 10 pages ----
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            # ---- fetch page ----
            try:
                data = _curl_post(_SEARCH_API, {"page": page, "pageSize": _PAGE_SIZE})
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] page {page} fetch error: {exc}, stopping")
                break

            if data is None:
                print(f"[{_SITE_ID}] page {page} returned None, stopping")
                break

            publications = data.get("publications") or []
            if not publications:
                print(f"[{_SITE_ID}] page {page}: empty publications list, done")
                break

            new_on_page = 0
            for pub in publications:
                if saved >= limit_or_inf:
                    break

                try:
                    link_url = pub.get("linkUrl") or ""
                    if link_url.startswith("/"):
                        full_url = _BASE_URL + link_url
                    else:
                        full_url = link_url

                    # dedup
                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    new_on_page += 1

                    title = (pub.get("title") or "").strip()
                    if not title:
                        continue

                    description = (pub.get("description") or "").strip()
                    if len(description) < _ABSTRACT_MIN:
                        print(
                            f"[{_SITE_ID}] skip '{title[:50]}': "
                            f"abstract too short ({len(description)} chars)"
                        )
                        continue

                    pub_date_raw = pub.get("publishedDate") or ""
                    pub_date = _parse_date(pub_date_raw)
                    topic = (pub.get("topic") or "").strip()
                    file_type = (pub.get("fileType") or "").upper()
                    file_size = pub.get("fileSize")
                    media_id = _extract_media_id(link_url)
                    original_filename = _extract_filename(link_url)
                    external_id = media_id or full_url
                    pdf_url = full_url if file_type == "PDF" else None

                    metadata = json.dumps({
                        "topic": topic,
                        "fileType": file_type,
                        "fileSize": file_size,
                        "linkUrl": link_url,
                        "isExternalLink": pub.get("isExternalLink"),
                        "publishedDate_raw": pub_date_raw,
                        "media_id": media_id,
                        "originalFilename": original_filename,
                    })

                    self._save_paper({
                        "site_id": _SITE_ID,
                        "external_id": external_id,
                        "post_number": None,
                        "title": title,
                        "abstract": description,
                        "published_date": pub_date or None,
                        "listed_date": pub_date or None,
                        "authors": None,
                        "publisher": "Skills Development Scotland",
                        "department": None,
                        "journal": None,
                        "url": full_url,
                        "pdf_url": pdf_url,
                        "keywords": topic or None,
                        "category": topic or None,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": metadata,
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    title_preview = (pub.get("title") or "")[:40]
                    print(f"[{_SITE_ID}] item '{title_preview}' failed: {exc}")
                    continue

            # if no new unique URLs appeared, pagination looped back to page 1
            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: no new URLs detected, pagination complete")
                break

            page += 1
            time.sleep(_RATE_SLEEP)

        print(f"[{_SITE_ID}] crawl complete: saved {saved} records")
        return saved
