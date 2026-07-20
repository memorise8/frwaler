# -*- coding: utf-8 -*-
"""Crawler for Institut national du cancer — Espace presse (cancer-fr-presse).

Starting URL:
    https://www.cancer.fr/presse?press_list%5Bfilters%5D%5Bdate_from%5D=
        &press_list%5Bfilters%5D%5Bdate_to%5D=&search=
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

_SITE_ID = "cancer-fr-presse"
_BASE_URL = "https://www.cancer.fr"
_LIST_BASE = "https://www.cancer.fr/presse"
_LIST_QUERY = (
    "press_list%5Bfilters%5D%5Bdate_from%5D="
    "&press_list%5Bfilters%5D%5Bdate_to%5D="
    "&search="
)
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 100
_WALL_CLOCK_SECS = 25 * 60  # 25 minutes

_MONTH_FR = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}

# Fallback parser chain: prefer html5lib for robustness, then lxml, then stdlib.
try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None  # type: ignore
    _PARSERS = []


def _make_soup(html: str):
    """Parse HTML with a fallback parser chain. Returns soup or None."""
    if not _BS:
        return None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _parse_date_fr(text: str) -> str | None:
    """Parse a French date string to ISO YYYY-MM-DD. Returns None if unparseable."""
    if not text:
        return None
    text = text.strip()
    # Already ISO
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return text[:10]
    # DD/MM/YYYY or DD.MM.YYYY
    m = re.match(r"(\d{1,2})[/.](\d{1,2})[/.](\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # "5 mai 2026" or "28 décembre 2025"
    m = re.match(r"(\d{1,2})\s+([a-zà-ÿ]+)\s+(\d{4})", text.lower())
    if m:
        month = _MONTH_FR.get(m.group(2))
        if month:
            return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"
    return None


class CancerFrPresseCrawler(BaseCrawler):
    site_id = "cancer-fr-presse"
    site_name = "Custom: cancer-fr-presse"
    base_url = "https://www.cancer.fr"

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        """Fetch URL via curl with 3 retries and exponential backoff."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 15, check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr[:200]}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed "
                    f"for {url}: {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str, page: int) -> tuple[list[str], int]:
        """Extract article URLs and total page count from a list page.

        Returns (urls, total_pages). total_pages is 0 if not found.
        """
        soup = _make_soup(html)
        if not soup:
            return [], 0

        # Total pages from <title>Espace presse | page (1/10)</title>
        total_pages = 0
        if page == 1:
            title_el = soup.find("title")
            if title_el:
                m = re.search(r"page\s*\((\d+)/(\d+)\)", title_el.get_text() or "")
                if m:
                    total_pages = int(m.group(2))

        # Collect all /presse/<slug> links (not bare /presse)
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.match(r"^/presse/[^?#]+$", href) and href not in seen:
                seen.add(href)
                urls.append(_BASE_URL + href)

        return urls, total_pages

    def _parse_detail(self, html: str, url: str) -> dict | None:
        """Extract all fields from a detail page. Returns paper dict or None."""
        soup = _make_soup(html)
        if not soup:
            return None

        # Title
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else None
        if not title:
            return None

        # "Mis à jour le" date — <time datetime="YYYY-MM-DD"> in article-section-date
        updated_date = None
        date_p = soup.find("p", class_="article-section-date")
        if date_p:
            time_el = date_p.find("time")
            if time_el and time_el.get("datetime"):
                updated_date = time_el["datetime"][:10]
            else:
                txt = re.sub(r"Mis\s+[àa]\s+jour\s+le\s*", "", date_p.get_text(strip=True), flags=re.I)
                updated_date = _parse_date_fr(txt)

        # Bold intro paragraph: "Publié le DD mois YYYY, [body...]"
        intro_div = soup.find(
            "div",
            class_=lambda c: c and "fw-bold" in c and "my-3" in c,
        )
        intro_text = intro_div.get_text(separator=" ", strip=True) if intro_div else ""

        # Published date from "Publié le" in intro
        published_date = None
        m_pub = re.search(
            r"Publi[ée]\s+le\s+(\d{1,2}\s+[a-zà-ÿ]+\s+\d{4}"
            r"|\d{1,2}[/.]\d{1,2}[/.]\d{4})",
            intro_text,
            re.I,
        )
        if m_pub:
            published_date = _parse_date_fr(m_pub.group(1))
        if not published_date:
            published_date = updated_date

        # Abstract: intro paragraph is the primary source
        abstract = intro_text

        # Extend with wysiwyg body when intro is absent or too short
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            wysiwyg = soup.find("div", class_="wysiwyg")
            if wysiwyg:
                body = wysiwyg.get_text(separator=" ", strip=True)
                abstract = (abstract + " " + body).strip() if abstract else body

        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(
                f"[{_SITE_ID}] abstract too short ({len(abstract)} chars) "
                f"for {url}; skipping"
            )
            return None

        # PDF URL and original filename from /content/download/ links
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/content/download/" in href:
                pdf_url = href if href.startswith("http") else _BASE_URL + href
                path_part = href.split("?")[0]
                tail = path_part.rstrip("/").split("/")[-1]
                if tail:
                    original_filename = urllib.parse.unquote(tail)
                break

        # Slug as external_id / post_number
        slug = url.rstrip("/").split("/presse/")[-1]

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": updated_date,
            "url": url,
            "pdf_url": pdf_url,
            "publisher": "Institut national du cancer",
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "posted_date": updated_date,
                    "originalFilename": original_filename,
                    "slug": slug,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Institut national du cancer press releases.

        Walks paginated list pages, fetches each detail page, and saves
        via self._save_paper(). Returns the count of saved records.
        """
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        limit_label = str(limit) if limit is not None else "∞"
        total_pages = 0

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget
            if time.time() - crawl_start > _WALL_CLOCK_SECS:
                print(f"[{_SITE_ID}] wall-clock budget reached; stopping.")
                break

            # Limit already met — stop before fetching the next list page
            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached.")

            # Build list URL
            list_url = (
                f"{_LIST_BASE}?{_LIST_QUERY}"
                + (f"&page={page}" if page > 1 else "")
            )

            raw = self._curl(list_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page}: fetch failed; stopping list walk.")
                break

            page_urls, pg_total = self._parse_list_page(raw, page)

            if page == 1 and pg_total:
                total_pages = pg_total
                print(f"[{_SITE_ID}] Total list pages: {total_pages}")

            if not page_urls:
                print(f"[{_SITE_ID}] page {page}: 0 items; end of list.")
                break

            # URL deduplication
            new_urls = [u for u in page_urls if u not in seen_urls]
            for u in new_urls:
                seen_urls.add(u)

            if not new_urls:
                print(f"[{_SITE_ID}] page {page}: no new URLs; possible loop — stopping.")
                break

            # Progress log every 10 list pages
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            # Fetch and save detail pages from this list page
            for url in new_urls:
                if limit is not None and saved >= limit:
                    break
                if time.time() - crawl_start > _WALL_CLOCK_SECS:
                    print(f"[{_SITE_ID}] wall-clock budget reached during detail fetch.")
                    break

                try:
                    raw_detail = self._curl(url)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {url} fetch failed; skipping.")
                        continue

                    paper = self._parse_detail(raw_detail, url)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    print(f"[{_SITE_ID}] Interrupted; stopping.")
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {url} failed: {exc}")
                    continue

                time.sleep(self._detail_delay)

            # Stop when we've reached the last known page
            if total_pages and page >= total_pages:
                break

        print(f"[{_SITE_ID}] Done. Saved {saved} records.")
        return saved
