# -*- coding: utf-8 -*-
"""Ministero degli Affari Esteri e della Cooperazione Internazionale
press-release (comunicati stampa) crawler.

Access strategy:
  The site sits behind Radware Bot Manager which fingerprints the TLS
  handshake — curl is blocked regardless of User-Agent.  We use Playwright
  (headless Chromium) which presents a real browser fingerprint and passes
  the challenge transparently.

  The WordPress REST API endpoint:
    GET /it/wp-json/wp/v2/posts
        ?categories=1   ← WP category ID for "Comunicati"
        &per_page=100
        &page=N
        &_fields=id,title,date,content,excerpt,link
        &orderby=date&order=desc

  Playwright wraps JSON responses in a <pre> element; we extract that.
"""

import html as _html_mod
import json
import re
import sys
import time

# Absolute imports — spec_from_file_location gives no package context, but
# the test harness inserts the project root into sys.path before loading us.
from crawler.base_crawler import BaseCrawler

_CATEGORY_ID = 1        # WP category ID for "Comunicati"
_PER_PAGE = 100         # posts per REST API page
_MAX_PAGES = 200        # safety cap (log + exit, not hard crash)
_BUDGET_SECS = 25 * 60  # 25-minute wall-clock budget


# ---------------------------------------------------------------------------
# HTML → plain text helpers
# ---------------------------------------------------------------------------

def _bs4_strip(html_str: str) -> str:
    """Strip HTML with BeautifulSoup; fallback chain html5lib→lxml→html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_str, parser)
            return soup.get_text(separator=" ", strip=True)
        except Exception:
            continue
    # Last resort: regex
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = _html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    try:
        text = _bs4_strip(raw)
    except Exception:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = _html_mod.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
    # Source titles sometimes carry double-encoded entities (e.g.
    # "&amp;#8211;" → a single HTML-parse pass leaves "&#8211;" as literal
    # text). Unescape twice to fully resolve them, then collapse whitespace.
    text = _html_mod.unescape(_html_mod.unescape(text))
    return " ".join(text.split())


def _parse_wp_date(raw: str) -> str:
    """Convert '2026-05-13T14:55:50' → '2026-05-13'."""
    if not raw:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class EsteriItItCrawler(BaseCrawler):
    """Crawler for MAECI (Italian MFA) comunicati stampa."""

    site_id = "esteri-it-it"
    site_name = "Custom: esteri-it-it"
    base_url = "https://www.esteri.it"

    _WP_API = "https://www.esteri.it/it/wp-json/wp/v2/posts"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_api_page(self, page: int) -> list:
        """Return a list of WP post dicts for *page* (1-based).

        Uses Playwright because Radware Bot Manager blocks curl by TLS
        fingerprint.  The JSON response arrives wrapped in a <pre> element.
        Retries up to 3× with exponential back-off (1 s, 3 s, 9 s).
        """
        from crawler.playwright_fetcher import fetch_html

        url = (
            f"{self._WP_API}"
            f"?categories={_CATEGORY_ID}"
            f"&per_page={_PER_PAGE}"
            f"&page={page}"
            f"&_fields=id,title,date,content,excerpt,link"
            f"&orderby=date&order=desc"
        )

        for attempt in range(3):
            try:
                html = fetch_html(
                    url,
                    timeout_seconds=50,
                    extra_wait_seconds=2.0,
                    block_resources=True,
                )
                if not html:
                    wait = [1, 3, 9][attempt]
                    print(
                        f"[esteri-it-it] Empty Playwright response page {page}, "
                        f"retry {attempt + 1}/3 in {wait}s"
                    )
                    time.sleep(wait)
                    continue

                # Playwright renders JSON pages as <pre>…</pre>
                pre = re.search(r"<pre[^>]*>(.*?)</pre>", html, re.DOTALL)
                if pre:
                    raw_json = pre.group(1).strip()
                    data = json.loads(raw_json)
                    if isinstance(data, list):
                        return data
                    # WP REST API returns a dict on error (e.g. 400)
                    code = data.get("code", "")
                    if "rest_post_invalid_page_number" in code:
                        return []  # past last page
                    print(f"[esteri-it-it] WP API error page {page}: {data}")
                    return []

                # Fallback: bare JSON body
                data = json.loads(html.strip())
                if isinstance(data, list):
                    return data
                return []

            except json.JSONDecodeError as exc:
                wait = [1, 3, 9][attempt]
                print(
                    f"[esteri-it-it] JSON decode error page {page} "
                    f"(attempt {attempt + 1}/3): {exc}"
                )
                time.sleep(wait)
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(
                    f"[esteri-it-it] API fetch error page {page} "
                    f"(attempt {attempt + 1}/3): {exc}"
                )
                time.sleep(wait)

        print(f"[esteri-it-it] Giving up on page {page} after 3 attempts.")
        return []

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MAECI comunicati via WP REST API.

        Parameters
        ----------
        limit : int or None
            Maximum records to save.  None = unlimited.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # --- Guards ---
            if time.time() - start_time > _BUDGET_SECS:
                print(
                    f"[esteri-it-it] 25-minute wall-clock budget reached "
                    f"at page {page}. Stopping cleanly."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(
                    f"[esteri-it-it] Safety cap of {_MAX_PAGES} pages reached. "
                    f"Stopping."
                )
                break

            if page % 10 == 0:
                print(
                    f"[esteri-it-it] page {page}: saved {saved}/{limit_str}"
                )

            # --- Fetch ---
            items = self._fetch_api_page(page)

            if not items:
                print(f"[esteri-it-it] No items returned for page {page}. Done.")
                break

            new_this_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    post_url = (item.get("link") or "").strip()

                    # URL-level dedup (guards against paginator re-sending items)
                    if post_url in seen_urls:
                        continue
                    seen_urls.add(post_url)
                    new_this_page += 1

                    post_id = item.get("id")
                    external_id = str(post_id) if post_id is not None else ""

                    title_field = item.get("title") or {}
                    if isinstance(title_field, dict):
                        title = _strip_html(title_field.get("rendered", ""))
                    else:
                        title = _strip_html(str(title_field))

                    raw_date = item.get("date", "")
                    published_date = _parse_wp_date(raw_date)

                    # Build abstract from full content (press releases are long)
                    content_field = item.get("content") or {}
                    content_html = (
                        content_field.get("rendered", "")
                        if isinstance(content_field, dict)
                        else str(content_field)
                    ) or ""

                    excerpt_field = item.get("excerpt") or {}
                    excerpt_html = (
                        excerpt_field.get("rendered", "")
                        if isinstance(excerpt_field, dict)
                        else str(excerpt_field)
                    ) or ""

                    content_text = _strip_html(content_html)
                    excerpt_text = _strip_html(excerpt_html)

                    # Prefer whichever is longer; fall back to combination
                    if len(content_text) >= len(excerpt_text):
                        abstract = content_text
                    else:
                        abstract = excerpt_text

                    if not abstract and excerpt_text:
                        abstract = excerpt_text

                    if len(abstract) < 50:
                        print(
                            f"[esteri-it-it] Skipping short abstract "
                            f"({len(abstract)} chars): {title[:50]}"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title or "(untitled)",
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": post_url,
                        "pdf_url": None,
                        "authors": "",
                        "publisher": (
                            "Ministero degli Affari Esteri e della "
                            "Cooperazione Internazionale"
                        ),
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": "Comunicati",
                        "doi": "",
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "post_id": post_id,
                                "posted_date": raw_date,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[esteri-it-it] Saved {saved}/{limit_str}: "
                        f"{title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[esteri-it-it] Item {item.get('id', '?')} failed: {exc}"
                    )
                    continue

            # Deduplication loop guard: if all URLs on this page were already
            # seen, the paginator has looped back to a visited page.
            if new_this_page == 0:
                print(
                    f"[esteri-it-it] No new URLs on page {page} "
                    f"(dedup loop guard). Stopping."
                )
                break

            page += 1
            # Playwright calls already take ~5–10 s each; minimal extra sleep.
            time.sleep(0.3)

        print(f"[esteri-it-it] Done. Total saved: {saved}")
        return saved
