# -*- coding: utf-8 -*-
"""Mental Health Commission of Canada — Resources crawler.

Starting URL : https://mentalhealthcommission.ca/resources/
Data source  : WordPress REST API, custom post type ``resource``
               https://mentalhealthcommission.ca/wp-json/wp/v2/resource
               (``_embed=1`` bundles taxonomy term names — category, program,
               program-area, audience, covid-hub-category, health-canada —
               directly into the listing response, so no separate detail
               fetch is required per item.)
~615 items, 100/page => ~7 pages at time of writing.
"""

import html
import json
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "mentalhealthcommission-ca-resources"
_BASE_URL = "https://mentalhealthcommission.ca"
_API_URL = "https://mentalhealthcommission.ca/wp-json/wp/v2/resource"
_PUBLISHER = "Mental Health Commission of Canada"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_PER_PAGE = 100


# ---------------------------------------------------------------------------
# Helpers (module-level, reusable without self)
# ---------------------------------------------------------------------------

def _make_soup(html_text: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html_text, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max 1.3 and exponential-backoff retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: application/json,text/html;q=0.9,*/*;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
        except Exception as exc:
            if attempt >= retries - 1:
                print(f"[{_SITE_ID}] curl error: {exc}")
                return None
        if attempt < retries - 1:
            wait = [1, 3, 9][attempt]
            print(f"[{_SITE_ID}] empty/error for {url}, retry {attempt + 1}/{retries} in {wait}s")
            time.sleep(wait)
    return None


def _html_to_text(content_html: str) -> str:
    """Strip HTML tags/entities down to plain text. Never raises."""
    if not content_html:
        return ""
    soup = _make_soup(content_html)
    if soup is None:
        # Last-resort fallback: crude tag stripping.
        text = re.sub(r"<[^>]+>", " ", content_html)
        text = html.unescape(text)
    else:
        text = soup.get_text(separator=" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _extract_terms(item: dict) -> tuple[list[str], list[str]]:
    """Return (category_names, all_term_names) from an ``_embed``ded item."""
    cat_names: list[str] = []
    all_names: list[str] = []
    embedded = item.get("_embedded") or {}
    for group in embedded.get("wp:term") or []:
        for term in group or []:
            name = (term or {}).get("name")
            if not name:
                continue
            all_names.append(name)
            if term.get("taxonomy") == "category":
                cat_names.append(name)
    return cat_names, all_names


def _extract_pdf(content_html: str) -> tuple[str | None, str | None]:
    """Find the first PDF link in rendered content. Returns (pdf_url, original_filename)."""
    if not content_html:
        return None, None
    match = re.search(r'href="([^"]+\.pdf[^"]*)"', content_html, re.IGNORECASE)
    if not match:
        return None, None
    url = html.unescape(match.group(1))
    if url.startswith("/"):
        url = _BASE_URL + url
    tail = urllib.parse.unquote(url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0])
    original_filename = tail if tail and "." in tail else None
    return url, original_filename


def _parse_date(raw: str | None) -> str:
    """WP ISO datetime ('2026-06-23T14:36:42') -> 'YYYY-MM-DD'. Never raises."""
    if not raw:
        return ""
    return raw[:10] if len(raw) >= 10 else raw


def _fetch_category_map() -> dict:
    """Best-effort fetch of the category taxonomy (id -> name). Returns {} on failure."""
    raw = _curl_get(f"{_BASE_URL}/wp-json/wp/v2/categories?per_page=100")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, list):
        return {}
    return {c["id"]: c.get("name") for c in data if isinstance(c, dict) and "id" in c}


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MentalhealthcommissionCaResourcesCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: mentalhealthcommission-ca-resources"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_display = limit if limit is not None else "∞"
        start_time = time.time()
        MAX_SECONDS = 300 * 60  # 2026-07-16 cap 확장 재수집: 25분 → 5시간
        MAX_PAGES = 200

        category_map = _fetch_category_map()

        page = 1
        while True:
            # --- Guards ---
            if limit is not None and saved >= limit:
                break
            if page > MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            # --- Fetch listing page (REST API, _embed bundles taxonomy terms) ---
            if page > 1:
                time.sleep(self._delay)

            params = urllib.parse.urlencode({
                "per_page": _PER_PAGE,
                "page": page,
                "_embed": 1,
                "orderby": "date",
                "order": "desc",
            })
            list_url = f"{_API_URL}?{params}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch listing page {page} ({list_url}). Stopping.")
                break

            try:
                items = json.loads(raw)
            except (ValueError, TypeError) as exc:
                print(f"[{_SITE_ID}] Failed to parse JSON on page {page}: {exc}. Stopping.")
                break

            if isinstance(items, dict):
                # WP REST error object (e.g. rest_post_invalid_page_number past the end).
                print(f"[{_SITE_ID}] API error on page {page}: {items.get('message', items)}. Done.")
                break

            if not items:
                print(f"[{_SITE_ID}] No records on page {page}. Done.")
                break

            # Deduplicate across pages (defends against a paginator looping back).
            new_items = [it for it in items if it.get("link") not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All URLs on page {page} already seen. Stopping.")
                break
            for it in new_items:
                seen_urls.add(it.get("link"))

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            # --- Process & save each item ---
            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_SECONDS:
                    print(f"[{_SITE_ID}] 25-minute budget reached mid-page. Stopping.")
                    break

                item_url = item.get("link") or ""
                try:
                    wp_id = item.get("id")
                    external_id = str(wp_id) if wp_id is not None else None

                    title = html.unescape((item.get("title") or {}).get("rendered") or "").strip()
                    content_html = (item.get("content") or {}).get("rendered") or ""
                    abstract = _html_to_text(content_html)

                    if not abstract or len(abstract) < 50:
                        print(f"[{_SITE_ID}] Abstract <50 chars for {item_url or wp_id}. Skipping.")
                        continue

                    date_raw = item.get("date") or ""
                    published_date = _parse_date(date_raw)
                    listed_date = published_date

                    cat_names, all_term_names = _extract_terms(item)
                    if not cat_names:
                        # Fall back to the (non-embedded) numeric category ids.
                        cat_names = [
                            category_map[c] for c in (item.get("categories") or [])
                            if c in category_map
                        ]
                    category = cat_names[0] if cat_names else ""
                    keywords = ", ".join(dict.fromkeys(all_term_names)) if all_term_names else ""

                    pdf_url, original_filename = _extract_pdf(content_html)

                    metadata = {
                        "posted_date": date_raw,
                        "originalFilename": original_filename,
                        "wp_id": wp_id,
                        "slug": item.get("slug"),
                        "modified": item.get("modified"),
                        "categories_raw": item.get("categories"),
                        "term_names": all_term_names,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title or "(untitled)",
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url or "",
                        "original_filename": original_filename or "",
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": "",
                        "journal": "",
                        "keywords": keywords,
                        "category": category,
                        "doi": "",
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_display}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_url or '?'} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
