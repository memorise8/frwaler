# -*- coding: utf-8 -*-
"""Crawler for ISS National Lab Press Releases.

Uses the WordPress REST API (category 5 = Press Releases, ~556 posts).
"""

import json
import re
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _BS4_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    BeautifulSoup = None
    _BS4_PARSERS = []


def _make_soup(html: str):
    if BeautifulSoup is None:
        return None
    for parser in _BS4_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


_HTML_ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
    "&#038;": "&", "&#8220;": "“", "&#8221;": "”",
    "&#8211;": "–", "&#8212;": "—", "&#8216;": "‘",
    "&#8217;": "’", "&#8230;": "…", "&ldquo;": "“",
    "&rdquo;": "”", "&lsquo;": "‘", "&rsquo;": "’",
    "&ndash;": "–", "&mdash;": "—", "&hellip;": "…",
}


def _strip_to_text(html: str) -> str:
    """Strip WP shortcodes and HTML tags; return clean plain text."""
    # Remove VC builder shortcodes: [vc_row css="..."]
    text = re.sub(r'\[/?[a-z_]+[^\]]*\]', ' ', html)
    soup = _make_soup(text)
    if soup is not None:
        text = soup.get_text(separator=" ")
    else:
        text = re.sub(r'<[^>]+>', ' ', text)
    # Decode HTML entities
    for ent, char in _HTML_ENTITIES.items():
        text = text.replace(ent, char)
    # Numeric entities
    text = re.sub(r'&#(\d+);', lambda m: chr(int(m.group(1))), text)
    text = re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m.group(1), 16)), text)
    return re.sub(r'\s+', ' ', text).strip()


class ISSNationalLabPressReleasesCrawler(BaseCrawler):
    site_id = "issnationallab-org-press-releases"
    site_name = "Custom: issnationallab-org-press-releases"
    base_url = "https://issnationallab.org"

    _API_BASE = "https://issnationallab.org/wp-json/wp/v2"
    _CATEGORY_ID = 5   # "Press Releases"
    _PER_PAGE = 100

    def _api_get(self, endpoint: str, params: dict):
        """GET WP REST API; return (list, total, total_pages) or ([], 0, 0)."""
        url = f"{self._API_BASE}/{endpoint}"
        # Override session headers to ensure JSON and avoid brotli decode issues
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Accept-Language": "en-US,en;q=0.9",
        }
        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, headers=headers, timeout=30)
                resp.raise_for_status()
                total = int(resp.headers.get("X-WP-Total", 0))
                total_pages = int(resp.headers.get("X-WP-TotalPages", 1))
                data = resp.json()
                if not isinstance(data, list):
                    print(f"[{self.site_id}] Unexpected response type: {type(data)}")
                    return [], 0, 0
                return data, total, total_pages
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] API error (attempt {attempt+1}/3): {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return [], 0, 0

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_SECONDS = 25 * 60
        SAFETY_CAP = 200

        page = 1
        known_total_pages = None

        while page <= SAFETY_CAP:
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget reached at page {page}, exiting cleanly.")
                break

            if saved >= limit_or_inf:
                break

            params = {
                "categories": self._CATEGORY_ID,
                "per_page": self._PER_PAGE,
                "page": page,
                "_fields": "id,title,excerpt,content,date,link",
                "orderby": "date",
                "order": "desc",
            }

            data, total, total_pages_resp = self._api_get("posts", params)

            if known_total_pages is None and total_pages_resp:
                known_total_pages = total_pages_resp
                print(f"[{self.site_id}] Total posts: {total}, pages: {known_total_pages}")

            if not data:
                print(f"[{self.site_id}] Empty response on page {page}, stopping pagination.")
                break

            new_on_page = 0
            for item in data:
                if saved >= limit_or_inf:
                    break

                try:
                    url = item.get("link", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    post_id = str(item.get("id", ""))

                    title_raw = item.get("title", {}).get("rendered", "")
                    title = _strip_to_text(title_raw)
                    if not title:
                        title = "(untitled)"

                    # Prefer full content; fall back to excerpt
                    content_html = item.get("content", {}).get("rendered", "")
                    abstract = _strip_to_text(content_html)
                    if len(abstract) < 50:
                        excerpt_html = item.get("excerpt", {}).get("rendered", "")
                        abstract = _strip_to_text(excerpt_html)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping post {post_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    date_str = item.get("date", "")
                    published_date = date_str[:10] if date_str else None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": post_id,
                        "title": title,
                        "authors": json.dumps([]),
                        "abstract": abstract,
                        "category": "Press Release",
                        "keywords": json.dumps([]),
                        "published_date": published_date,
                        "url": url,
                        "pdf_url": None,
                        "doi": None,
                        "department": None,
                        "metadata": json.dumps({"wp_post_id": post_id}),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('id', '?')} failed: {exc}")
                    continue

            if page == SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages reached, exiting.")

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            if new_on_page == 0:
                print(f"[{self.site_id}] No new records on page {page}, stopping.")
                break

            if known_total_pages and page >= known_total_pages:
                break

            page += 1
            time.sleep(1.0)

        print(f"[{self.site_id}] Done. Saved {saved} press releases.")
        return saved
