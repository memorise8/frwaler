# -*- coding: utf-8 -*-
"""Crawler for Medlingsinstitutet archive (mi.se/arkiv/?type=post).

Uses the WordPress REST API to enumerate publications, referral responses, and
news posts.  PDF URLs are extracted from detail pages for the publication type.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.mi.se"
_API_BASE = _BASE_URL + "/wp-json/wp/v2"

# Post types to crawl in priority order; tuple = (wp_rest_endpoint, category_label)
_POST_TYPES = [
    ("publication",       "Publikation"),
    ("referral-response", "Remissvar"),
    ("posts",             "Nyhet"),
]

_PER_PAGE      = 100
_MAX_PAGES     = 200
_WALL_MINUTES  = 25
_MIN_ABSTRACT  = 50   # skip & log items whose abstract is shorter
_DETAIL_SLEEP  = 1.0
_PUBLISHER     = "Medlingsinstitutet"
_FIELDS        = "id,date,slug,link,title,excerpt,content,yoast_head_json"


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl(url: str, retries: int = 3) -> str:
    """Fetch *url* with curl (TLS-1.3, insecure). Returns decoded body or '' on failure."""
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(backoff[attempt - 1])
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-skL",
                    "--max-time", "30",
                    "--user-agent",
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "-H", "Accept: application/json, text/html, */*",
                    "-H", "Accept-Language: sv-SE,sv;q=0.9,en;q=0.8",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[mi-se-arkiv] curl exit {result.returncode} for {url}")
        except subprocess.TimeoutExpired:
            print(
                f"[mi-se-arkiv] curl timeout "
                f"(attempt {attempt + 1}/{retries}) for {url}"
            )
        except Exception as exc:
            print(
                f"[mi-se-arkiv] curl error "
                f"(attempt {attempt + 1}/{retries}): {exc}"
            )
    return ""


def _curl_json(url: str, retries: int = 3):
    """Fetch *url* and parse as JSON.  Returns (parsed, raw_str) or (None, '')."""
    raw = _curl(url, retries=retries)
    if not raw:
        return None, ""
    try:
        return json.loads(raw), raw
    except json.JSONDecodeError as exc:
        print(f"[mi-se-arkiv] JSON parse error for {url}: {exc}")
        return None, raw


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
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


def _strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Detail-page PDF extraction
# ---------------------------------------------------------------------------

def _get_pdf_url(detail_url: str) -> tuple[str, str]:
    """Fetch *detail_url* and return (pdf_url, original_filename) or ('', '')."""
    raw = _curl(detail_url)
    if not raw:
        return "", ""

    soup = _make_soup(raw)
    if soup is not None:
        try:
            for tag in soup.find_all("a", href=True):
                href = tag["href"]
                if ".pdf" in href.lower():
                    full = href if href.startswith("http") else _BASE_URL + href
                    fname = full.rstrip("/").split("/")[-1].split("?")[0]
                    return full, fname
        except Exception:
            pass

    # regex fallback for malformed pages
    m = re.search(r'href="([^"]*\.pdf)"', raw, re.IGNORECASE)
    if m:
        href = m.group(1)
        full = href if href.startswith("http") else _BASE_URL + href
        fname = full.rstrip("/").split("/")[-1].split("?")[0]
        return full, fname

    return "", ""


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MiSeArkivCrawler(BaseCrawler):
    site_id   = "mi-se-arkiv"
    site_name = "Custom: mi-se-arkiv"
    base_url  = "https://www.mi.se"

    # ------------------------------------------------------------------
    # WP REST API list fetcher
    # ------------------------------------------------------------------

    def _api_page(self, endpoint: str, page: int) -> list | None:
        """Fetch one WP API page for *endpoint*.

        Returns:
            list of item dicts on success,
            [] when pagination is exhausted (WP 400 / empty),
            None on network error.
        """
        url = (
            f"{_API_BASE}/{endpoint}"
            f"?per_page={_PER_PAGE}&page={page}&_fields={_FIELDS}"
        )
        data, _ = _curl_json(url)
        if data is None:
            return None
        # WP returns {"code": "rest_post_invalid_page_number", ...} past the last page
        if isinstance(data, dict):
            return []
        if not isinstance(data, list):
            return None
        return data

    # ------------------------------------------------------------------
    # Abstract extraction
    # ------------------------------------------------------------------

    def _extract_abstract(self, item: dict) -> str:
        """Return the best available abstract text for an API item."""
        # 1. excerpt.rendered (primary, most concise)
        exc = item.get("excerpt") or {}
        if isinstance(exc, dict):
            text = _strip_html(exc.get("rendered", ""))
        else:
            text = ""

        if len(text) >= 100:
            return text

        # 2. yoast meta description (SEO excerpt — often the same as excerpt)
        yoast = item.get("yoast_head_json") or {}
        desc = yoast.get("description") or yoast.get("og_description") or ""
        if len(desc) >= 100:
            return desc

        # 3. content.rendered (full body — cap at 2 000 chars)
        cnt = item.get("content") or {}
        if isinstance(cnt, dict):
            content_text = _strip_html(cnt.get("rendered", ""))
            if len(content_text) >= 100:
                return content_text[:2000]

        # Return best available even if short (caller decides whether to skip)
        return text or desc

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved          = 0
        seen_urls: set = set()
        limit_display  = limit if limit is not None else "inf"
        deadline       = time.time() + _WALL_MINUTES * 60

        for post_type, category in _POST_TYPES:
            if limit is not None and saved >= limit:
                break
            if time.time() > deadline:
                print(f"[mi-se-arkiv] {_WALL_MINUTES}-minute budget reached, stopping.")
                break

            print(f"[mi-se-arkiv] --- post type: {post_type} ---")

            for page in range(1, _MAX_PAGES + 1):
                if time.time() > deadline:
                    print(
                        f"[mi-se-arkiv] wall-clock budget reached "
                        f"at {post_type} page {page}, stopping."
                    )
                    break

                if page == _MAX_PAGES:
                    print(
                        f"[mi-se-arkiv] safety cap of {_MAX_PAGES} pages "
                        f"reached for {post_type}."
                    )

                if limit is not None and saved >= limit:
                    break

                if page == 1 or page % 10 == 0:
                    print(
                        f"[mi-se-arkiv] {post_type} page {page}: "
                        f"saved {saved}/{limit_display}"
                    )

                items = self._api_page(post_type, page)
                if items is None:
                    print(
                        f"[mi-se-arkiv] {post_type} page {page}: "
                        f"API error, stopping this type."
                    )
                    break
                if not items:
                    print(
                        f"[mi-se-arkiv] {post_type} page {page}: "
                        f"empty result, end of pagination."
                    )
                    break

                for item in items:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() > deadline:
                        break

                    try:
                        item_url = item.get("link", "")
                        if item_url in seen_urls:
                            continue
                        seen_urls.add(item_url)

                        wp_id = item.get("id")
                        slug  = item.get("slug") or str(wp_id)
                        pub_date = (item.get("date") or "")[:10] or None

                        title_raw = item.get("title") or {}
                        title = _strip_html(
                            title_raw.get("rendered", "")
                            if isinstance(title_raw, dict)
                            else str(title_raw)
                        )
                        if not title:
                            print(f"[mi-se-arkiv] item {wp_id}: no title, skipping.")
                            continue

                        abstract = self._extract_abstract(item)
                        if len(abstract) < _MIN_ABSTRACT:
                            print(
                                f"[mi-se-arkiv] item {wp_id} ({title[:40]}): "
                                f"abstract too short ({len(abstract)} chars), skipping."
                            )
                            continue

                        # PDF: only publication detail pages reliably have PDFs
                        pdf_url           = ""
                        original_filename = ""
                        if post_type == "publication":
                            time.sleep(_DETAIL_SLEEP)
                            pdf_url, original_filename = _get_pdf_url(item_url)

                        self._save_paper({
                            "site_id":           self.site_id,
                            "external_id":       slug,
                            "post_number":       str(wp_id),
                            "title":             title,
                            "abstract":          abstract,
                            "published_date":    pub_date,
                            "listed_date":       pub_date,
                            "url":               item_url,
                            "pdf_url":           pdf_url or None,
                            "publisher":         _PUBLISHER,
                            "category":          category,
                            "original_filename": original_filename or None,
                            "metadata": json.dumps(
                                {
                                    "wp_id":       wp_id,
                                    "slug":        slug,
                                    "post_type":   post_type,
                                    "posted_date": item.get("date", ""),
                                },
                                ensure_ascii=False,
                            ),
                        })
                        saved += 1
                        print(f"[mi-se-arkiv] saved [{saved}] {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[mi-se-arkiv] item {item.get('id', '?')} failed: {exc}"
                        )
                        continue

        print(f"[mi-se-arkiv] crawl complete: {saved} records saved.")
        return saved
