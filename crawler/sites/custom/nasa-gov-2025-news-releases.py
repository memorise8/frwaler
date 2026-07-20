# -*- coding: utf-8 -*-
"""NASA 2025 News Releases crawler — WordPress REST API (press-release post type).

Endpoint: https://www.nasa.gov/wp-json/wp/v2/press-release
Filters:  after=2025-01-01, before=2026-01-01  (~236 posts, 3 pages @ 100/page)
"""

import json
import re
import subprocess
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler

_SITE_ID = "nasa-gov-2025-news-releases"
_API_URL = "https://www.nasa.gov/wp-json/wp/v2/press-release"
_YEAR_AFTER = "2025-01-01T00:00:00"
_YEAR_BEFORE = "2026-01-01T00:00:00"
_PER_PAGE = 100
_MAX_PAGES = 200
_BUDGET_SECS = 25 * 60  # 25 minutes


def _strip_html(html: str) -> str:
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url: str, params: dict, site_id: str = _SITE_ID) -> str | None:
    """GET via curl with exponential-backoff retries (1s, 3s, 9s)."""
    qs = urlencode(params)
    full_url = f"{url}?{qs}"
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", full_url],
                capture_output=True, text=True, timeout=35,
            )
            if result.stdout.strip():
                return result.stdout
        except Exception as exc:
            print(f"[{site_id}] curl error (attempt {attempt + 1}/3): {exc}")
        if attempt < 2:
            wait = [1, 3, 9][attempt]
            print(f"[{site_id}] retrying in {wait}s...")
            time.sleep(wait)
    return None


class NasaGov2025NewsReleasesCrawler(BaseCrawler):
    site_id = "nasa-gov-2025-news-releases"
    site_name = "Custom: nasa-gov-2025-news-releases"
    base_url = "https://www.nasa.gov"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            elapsed = time.time() - start_time
            if elapsed > _BUDGET_SECS:
                print(f"[{self.site_id}] Time budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            params = {
                "per_page": _PER_PAGE,
                "page": page,
                "after": _YEAR_AFTER,
                "before": _YEAR_BEFORE,
                "_fields": "id,date,slug,title,excerpt,link,content",
            }

            raw = _curl_get(_API_URL, params, self.site_id)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON error on page {page}: {exc}. Stopping.")
                break

            if not isinstance(items, list) or not items:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    url = item.get("link", "")
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    post_id = item.get("id")
                    slug = item.get("slug", "")
                    date_raw = item.get("date", "")
                    published_date = date_raw[:10] if date_raw else None

                    title = _strip_html(item.get("title", {}).get("rendered", ""))
                    if not title:
                        print(f"[{self.site_id}] Skipping post {post_id}: no title")
                        continue

                    # Prefer excerpt (~300-400 chars); fall back to full content
                    excerpt_raw = (item.get("excerpt") or {}).get("rendered", "") or ""
                    content_raw = (item.get("content") or {}).get("rendered", "") or ""
                    abstract = _strip_html(excerpt_raw)
                    if len(abstract) < 50:
                        abstract = _strip_html(content_raw)[:3000]

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping post {post_id}: "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    metadata_dict = {
                        "posted_date": date_raw,
                        "post_id": post_id,
                        "slug": slug,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": str(post_id),
                        "post_number": str(post_id),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": url,
                        "pdf_url": None,
                        "authors": "",
                        "publisher": "NASA",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "News Release",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('id', '?')} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen. Stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
