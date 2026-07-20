# -*- coding: utf-8 -*-
"""Crawler for Carabinieri Comunicati Stampa (Italian Carabinieri press releases)."""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "carabinieri-it-in-vostro-aiuto"
_BASE_URL = "https://www.carabinieri.it"
_LIST_BASE = "https://www.carabinieri.it/in-vostro-aiuto/informazioni/comunicati-stampa"
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 50
_WALL_CLOCK_MAX = 25 * 60  # seconds


def _make_soup(html: str) -> BeautifulSoup:
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


def _strip_tags(html_frag: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_frag)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> str:
    """Convert 'DD/MM/YYYY ...' → 'YYYY-MM-DD'. Returns '' on failure."""
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", raw.strip())
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


class CarabinieriItInVostroAiutoCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: carabinieri-it-in-vostro-aiuto"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        for attempt in range(retries):
            try:
                cmd = [
                    "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                    "-H", f"User-Agent: {self.USER_AGENT}",
                    "-H", "Accept-Language: it-IT,it;q=0.9,en-US;q=0.8",
                    url,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.stdout and result.stdout.strip():
                    return result.stdout.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{_SITE_ID}] Empty response for {url[:80]}, retry in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = (attempt + 1) * 3
                    print(f"[{_SITE_ID}] curl error: {exc}, retry in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parser
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of dicts with url, title, raw_date, listed_date, source, summary."""
        soup = _make_soup(html)
        items = []
        seen = set()

        # The list is rendered as a plain table; rows contain tabellaP or tabellaD cells.
        # Each data row has 4 tds: date | link (location+title) | source | summary
        for row in soup.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 2:
                continue

            # First td: raw date string
            raw_date = tds[0].get_text(" ", strip=True)
            listed_date = _parse_date(raw_date)
            if not listed_date:
                continue  # not an article row

            # Second td: link with title
            a_tag = tds[1].find("a", href=True)
            if not a_tag:
                continue
            url = a_tag.get("href", "").strip()
            if not url.startswith("http"):
                url = urljoin(_BASE_URL, url)
            if url in seen:
                continue
            seen.add(url)

            # Title: full text of the link, stripping the location prefix span
            title = a_tag.get_text(" ", strip=True)
            # Remove "Location - " prefix (the lblLuogo span adds "City - " prefix)
            loc_span = a_tag.find("span")
            if loc_span:
                loc_text = loc_span.get_text(" ", strip=True)
                if title.startswith(loc_text):
                    title = title[len(loc_text):].strip()
                loc_span.decompose()
                title = a_tag.get_text(" ", strip=True)

            # Third td: source/publisher
            source = tds[2].get_text(" ", strip=True) if len(tds) > 2 else ""

            # Fourth td: summary snippet
            summary = tds[3].get_text(" ", strip=True) if len(tds) > 3 else ""
            # Strip trailing "..." added by the server
            summary = re.sub(r"\.\.\.$", "", summary).strip()

            items.append({
                "url": url,
                "title": title,
                "raw_date": raw_date,
                "listed_date": listed_date,
                "source": source,
                "summary": summary,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str) -> tuple[str, str, str]:
        """Return (abstract, published_date, location) from a detail page."""
        soup = _make_soup(html)

        # Title from og:title (first meta with property=og:title is the article title)
        og_title = ""
        for meta in soup.find_all("meta"):
            prop = meta.get("property", "") or meta.get("name", "")
            if "og:title" in prop:
                og_title = meta.get("content", "").strip()
                if og_title and og_title.lower() not in ("comunicati stampa", "carabinieri"):
                    break

        # Abstract from docTesto div (full body text — typically 500-2000 chars)
        abstract = ""
        doc_testo = soup.find(class_="docTesto")
        if doc_testo:
            abstract = doc_testo.get_text(" ", strip=True)
            abstract = unescape(abstract)
            abstract = re.sub(r"\s+", " ", abstract).strip()

        # Fallback: og:description
        if len(abstract) < 50:
            for meta in soup.find_all("meta"):
                prop = meta.get("property", "") or meta.get("name", "")
                if "og:description" in prop:
                    abstract = meta.get("content", "").strip()
                    break

        # Date and location from docData div
        published_date = ""
        location = ""
        doc_data = soup.find(class_="docData")
        if doc_data:
            data_text = doc_data.get_text(" ", strip=True)
            published_date = _parse_date(data_text)
            # Location from lblLuogo span
            loc_span = doc_data.find(id=re.compile(r"lblLuogo"))
            if loc_span:
                location = loc_span.get_text(" ", strip=True)

        return abstract, published_date, location

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        seen_urls: set[str] = set()
        saved = 0
        page = 1
        start_time = time.time()

        while True:
            # Limit check
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Wall-clock budget
            if time.time() - start_time > _WALL_CLOCK_MAX:
                print(f"[{_SITE_ID}] 25-minute budget reached. Stopping.")
                break

            # Build list page URL
            if page == 1:
                list_url = f"{_LIST_BASE}?d=all"
            else:
                list_url = f"{_LIST_BASE}/!page/page/{page}?d=all"

            # Progress log every 10 pages
            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            html = self._curl_get(list_url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page}. Done.")
                break

            # Deduplicate against already-seen URLs
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] Page {page} has no new URLs. Stopping.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(item["url"])

                try:
                    time.sleep(self._delay)

                    detail_html = self._curl_get(item["url"])
                    abstract = ""
                    published_date = item["listed_date"]
                    location = ""

                    if detail_html:
                        abstract, detail_date, location = self._parse_detail(detail_html)
                        if detail_date:
                            published_date = detail_date

                    # Fallback abstract from list summary
                    if not abstract or len(abstract) < _ABSTRACT_MIN_CHARS:
                        abstract = item.get("summary", "")

                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars) for {item['url'][:70]}, skipping.")
                        continue

                    # URL slug as external_id / post_number (no numeric IDs on this site)
                    slug = item["url"].rstrip("/").split("/")[-1]

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": slug,
                        "post_number": slug,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": item["listed_date"],
                        "url": item["url"],
                        "pdf_url": None,
                        "publisher": item.get("source", ""),
                        "department": None,
                        "authors": None,
                        "journal": None,
                        "keywords": None,
                        "category": "Comunicati Stampa",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": item["raw_date"],
                            "location": location,
                            "list_summary": item.get("summary", ""),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item['url'][:70]} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
