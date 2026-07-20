# -*- coding: utf-8 -*-
"""Crawler for JIC (John Innes Centre) News & Events via WordPress REST API.

Endpoint: GET https://www.jic.ac.uk/wp-json/wp/v2/posts?per_page=100&page=N&_embed=1
Total:    ~1126 posts across 12 pages (as of 2026-05-14).
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "jic-ac-uk-news-events"
_API_URL = "https://www.jic.ac.uk/wp-json/wp/v2/posts"
_PER_PAGE = 100
_MAX_PAGES = 200
_BUDGET_SECS = 25 * 60  # 25 minutes wall-clock cap


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _strip_html_regex(html: str) -> str:
    """Fast regex-based HTML stripper; used as final fallback."""
    text = re.sub(r"<[^>]+>", " ", html or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&[a-zA-Z0-9]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(html: str) -> str:
    """Strip HTML with BeautifulSoup (html5lib → lxml → html.parser), then regex."""
    if not html:
        return ""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, parser)
            text = re.sub(r"\s+", " ", soup.get_text(" ")).strip()
            return text
        except Exception:
            continue
    return _strip_html_regex(html)


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, params: dict) -> str | None:
    """GET via curl with 3-attempt exponential backoff (1s, 3s, 9s)."""
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    full_url = f"{url}?{qs}"
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept: application/json",
        "-H", "User-Agent: Mozilla/5.0 (compatible; JICCrawler/1.0)",
        full_url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace").strip()
            if raw:
                return raw
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/3): {exc}")
        if attempt < 2:
            wait = [1, 3, 9][attempt]
            print(f"[{_SITE_ID}] retrying in {wait}s...")
            time.sleep(wait)
    print(f"[{_SITE_ID}] all 3 curl attempts failed for {full_url}")
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class JicAcUkNewsEventsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: jic-ac-uk-news-events"
    base_url = "https://www.jic.ac.uk"

    def crawl(self, limit=None):
        """Crawl JIC news & events via WP REST API.

        Paginates through /wp-json/wp/v2/posts with _embed=1 to get
        embedded category/tag terms without extra requests.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = limit if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget guard
            elapsed = time.time() - start_time
            if elapsed > _BUDGET_SECS:
                print(f"[{_SITE_ID}] 25-minute budget reached after {elapsed:.0f}s, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; logging and exiting.")

            time.sleep(self._delay)

            raw = _curl_get(_API_URL, {
                "per_page": _PER_PAGE,
                "page": page,
                "_embed": "1",
                "orderby": "date",
                "order": "desc",
            })

            if raw is None:
                print(f"[{_SITE_ID}] page {page}: fetch failed after retries, stopping.")
                break

            try:
                posts = json.loads(raw)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"[{_SITE_ID}] page {page}: JSON decode failed ({exc}), stopping.")
                break

            if not isinstance(posts, list) or not posts:
                print(f"[{_SITE_ID}] page {page}: no more posts. Done.")
                break

            new_on_page = 0

            for post in posts:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_url = post.get("link", "") or ""

                    # URL-based deduplication — prevents infinite loops if API paginates back
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)
                    new_on_page += 1

                    wp_id = post.get("id")
                    post_number = str(wp_id) if wp_id is not None else None
                    external_id = post_number

                    # Title
                    title_raw = (post.get("title") or {}).get("rendered") or ""
                    title = _strip_html(title_raw) or "(untitled)"

                    # Dates — WP returns ISO 8601: "2026-05-11T14:15:05+00:00"
                    date_raw = post.get("date") or ""
                    published_date = date_raw[:10] if len(date_raw) >= 10 else None
                    listed_date = published_date

                    # Abstract: excerpt preferred; fall back to full content
                    excerpt_html = (post.get("excerpt") or {}).get("rendered") or ""
                    content_html = (post.get("content") or {}).get("rendered") or ""

                    abstract = _strip_html(excerpt_html)
                    if len(abstract) < 50:
                        abstract = _strip_html(content_html)[:4000]

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] skip (abstract {len(abstract)} chars): {title[:60]}")
                        continue

                    # Embedded terms — _embed=1 inlines categories and tags
                    embedded = post.get("_embedded") or {}
                    term_groups = embedded.get("wp:term") or []
                    cat_names: list[str] = []
                    tag_names: list[str] = []
                    for group in term_groups:
                        for term in (group or []):
                            tax = term.get("taxonomy") or ""
                            name = term.get("name") or ""
                            if not name:
                                continue
                            if tax == "category":
                                cat_names.append(name)
                            elif tax == "post_tag":
                                tag_names.append(name)

                    category = ", ".join(cat_names) if cat_names else None
                    keywords = ", ".join(tag_names) if tag_names else None
                    slug = post.get("slug") or ""

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": post_url,
                        "pdf_url": None,
                        "authors": None,
                        "publisher": "John Innes Centre",
                        "department": None,
                        "journal": None,
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "wp_id": wp_id,
                            "slug": slug,
                            "post_type": post.get("type"),
                            "format": post.get("format"),
                            "categories": cat_names,
                            "tags": tag_names,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_display}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({exc}); continuing")
                    continue

            # No new URLs on this page → paginator has looped or site returned duplicates
            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page}: no new URLs found, pagination exhausted.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
