# -*- coding: utf-8 -*-
"""DPMC New Zealand — Department of the Prime Minister and Cabinet publications crawler.

List pages at /publications?page=N are served without Cloudflare protection.
Individual publication detail pages are protected by a CF managed challenge,
so this crawler relies on the teaser snippets from the list page as abstracts
and gracefully falls back when detail pages are inaccessible.
"""

import json
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler

_SITE_ID = "dpmc-govt-nz-publications"
_BASE_URL = "https://www.dpmc.govt.nz"
_LIST_URL = "https://www.dpmc.govt.nz/publications"
_PUBLISHER = "Department of the Prime Minister and Cabinet"
_MIN_ABSTRACT = 100  # skip items whose final abstract is shorter than this


def _bs4_parse(html: str):
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


def _is_cf_block(html: str) -> bool:
    """Return True if the HTML is a Cloudflare challenge page (not just a page with CF scripts).

    Note: legitimate DPMC pages include /cdn-cgi/challenge-platform scripts, so we
    cannot use 'challenge-platform' as a detection string — it appears on real pages too.
    """
    return (
        "Just a moment" in html
        or "window._cf_chl_opt" in html
        or "Enable JavaScript and cookies" in html
        or (html.strip().startswith("<!DOCTYPE") and "<title>Just a moment" in html)
    )


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#[0-9]+;", "", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


class DpmcGovtNzPublicationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: dpmc-govt-nz-publications"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with exponential backoff (1 s / 3 s / 9 s).

        Returns decoded response text, or None after all retries fail.
        A Cloudflare challenge page (HTTP 403 body) is returned as-is so the
        caller can detect it with _is_cf_block() and fall back gracefully.
        """
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-NZ,en;q=0.9",
            "-H", "Connection: keep-alive",
            url,
        ]
        backoff = [1, 3, 9]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
            if attempt < retries - 1:
                time.sleep(backoff[attempt])
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl DPMC publications list pages and save records.

        Paginates through /publications?page=N until the limit is reached,
        no new items appear, or the safety caps are hit.
        """
        saved = 0
        seen_urls: set = set()
        page = 0
        MAX_PAGES = 200
        start_time = time.time()
        MAX_SECONDS = 25 * 60
        limit_display = str(limit) if limit is not None else "∞"

        while True:
            # --- hard stops ---
            if limit is not None and saved >= limit:
                break
            if page >= MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping cleanly.")
                break

            # --- fetch list page ---
            list_url = f"{_LIST_URL}?page={page}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break
            if _is_cf_block(raw):
                print(f"[{_SITE_ID}] Cloudflare block on list page {page}. Stopping.")
                break

            # --- parse ---
            soup = _bs4_parse(raw)
            if soup is None:
                print(f"[{_SITE_ID}] HTML parse failed on page {page}. Skipping.")
                page += 1
                continue

            slats = soup.find_all("div", class_=re.compile(r"\bslat\b"))
            slats = [s for s in slats if "node" in " ".join(s.get("class", []))]
            if not slats:
                print(f"[{_SITE_ID}] No items found on page {page}. Done.")
                break

            # --- process items ---
            new_on_page = 0
            for slat in slats:
                if limit is not None and saved >= limit:
                    break

                try:
                    result = self._parse_slat(slat, seen_urls, page)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Slat parse error on page {page}: {exc}")
                    continue

                if result is None:
                    continue
                new_on_page += 1

                # --- try to enrich from detail page ---
                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(result["url"], retries=1)
                    if detail_raw and not _is_cf_block(detail_raw):
                        self._enrich_from_detail(result, detail_raw)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Detail fetch error for {result.get('url', '?')}: {exc}")

                # --- skip short abstracts ---
                abstract = result.get("abstract", "")
                if len(abstract) < _MIN_ABSTRACT:
                    print(f"[{_SITE_ID}] Skip (abstract {len(abstract)} chars): {result['title'][:60]}")
                    continue

                # --- save ---
                try:
                    self._save_paper(result)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_display}: {result['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Save error for {result.get('title', '?')[:60]}: {exc}")
                    continue

            # --- pagination logic ---
            if new_on_page == 0:
                print(f"[{_SITE_ID}] No new items on page {page}. Done.")
                break

            if page % 10 == 0 and page > 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-item helpers
    # ------------------------------------------------------------------

    def _parse_slat(self, slat, seen_urls: set, page: int):
        """Extract fields from a single list-page slat div.

        Returns a paper dict on success, None if the item should be skipped
        (missing title/URL, or already seen).
        """
        # title + URL
        title_a = slat.select_one("h2.slat__title a")
        if not title_a:
            return None
        title = title_a.get_text(strip=True)
        if not title:
            return None
        href = title_a.get("href", "")
        if not href:
            return None
        url = urljoin(_BASE_URL, href)
        if url in seen_urls:
            return None
        seen_urls.add(url)

        slug = urlparse(url).path.rstrip("/").split("/")[-1]

        # category
        badge = slat.select_one("span.badge")
        category = badge.get_text(strip=True) if badge else ""

        # date — use the datetime attribute from <time>
        time_el = slat.select_one("time")
        listed_date = ""
        if time_el:
            dt = time_el.get("datetime", "")
            if dt:
                listed_date = dt[:10]  # YYYY-MM-DD

        # abstract snippet from list page
        body_div = slat.select_one("div.slat__body")
        abstract = ""
        if body_div:
            abstract = body_div.get_text(separator=" ", strip=True)
        if not abstract:
            # fall back to stripping the whole slat's body text
            abstract = _strip_html(str(slat))

        return {
            "id": None,
            "site_id": _SITE_ID,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": listed_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": None,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": None,
            "journal": None,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": None,
            "metadata": json.dumps({
                "posted_date": listed_date,
                "originalFilename": None,
                "slug": slug,
                "node_id": None,
                "category": category,
            }, ensure_ascii=False),
        }

    def _enrich_from_detail(self, paper: dict, html: str) -> None:
        """Attempt to enrich a paper dict in-place from a detail page HTML.

        Upgrades abstract (if longer), extracts node ID and PDF URL.
        Silently ignores any parse errors.
        """
        soup = _bs4_parse(html)
        if soup is None:
            return

        # node ID from shortlink
        shortlink = soup.find("link", rel="shortlink")
        node_id = None
        if shortlink:
            m = re.search(r"/node/(\d+)", shortlink.get("href", ""))
            if m:
                node_id = m.group(1)
                # promote numeric node ID as the dedup key
                paper["external_id"] = node_id
                paper["post_number"] = node_id

        # full abstract — try several selectors in priority order
        for sel in (
            "div.field--name-body",
            "div.field--type-text-with-summary",
            "div.prose",
            "div.resource__body",
            "div.page__content",
        ):
            found = soup.select_one(sel)
            if found:
                candidate = found.get_text(separator=" ", strip=True)
                if len(candidate) > len(paper.get("abstract", "")):
                    paper["abstract"] = candidate
                break

        # PDF URL — first .pdf link on the page
        pdf_url = None
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.search(r"\.pdf", h, re.I):
                pdf_url = urljoin(_BASE_URL, h)
                break
        if pdf_url:
            paper["pdf_url"] = pdf_url
            fn = urlparse(pdf_url).path.rstrip("/").split("/")[-1].split("?")[0]
            if fn:
                paper["original_filename"] = fn

        # update metadata
        try:
            meta = json.loads(paper.get("metadata") or "{}")
        except (ValueError, TypeError):
            meta = {}
        meta["node_id"] = node_id
        if paper.get("original_filename"):
            meta["originalFilename"] = paper["original_filename"]
        paper["metadata"] = json.dumps(meta, ensure_ascii=False)
