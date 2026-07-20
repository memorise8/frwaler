# -*- coding: utf-8 -*-
"""Crawler for civilprotection.gov.gr Δελτία Τύπου (press releases / announcements).

Starting URL:
  https://civilprotection.gov.gr/deltia-tupou?filter_content_type[]=deltio_typoy
    &field_omilia_value[]=2&field_omilia_value_op=not

The site runs Drupal 10 behind the Akamai WAF which blocks standard curl and
plain requests. We use curl_cffi (Chrome120 TLS impersonation) to bypass the
WAF at the TLS-fingerprint level.

List endpoint: HTML pages with 8 items each, paginated via ?page=N%2C0
Detail page:   Drupal node HTML — body in .deltio-tupou-wrapper > .dt-content
               Node ID in <link rel="shortlink" href="/node/NNNNN">
"""

from __future__ import annotations

import json
import re
import time
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

try:
    from curl_cffi import requests as _cffi_requests
    _CFFI_AVAILABLE = True
except ImportError:
    _CFFI_AVAILABLE = False

_GREEK_MONTHS = {
    'Ιανουάριος': '01', 'Ιανουαρίου': '01',
    'Φεβρουάριος': '02', 'Φεβρουαρίου': '02',
    'Μάρτιος': '03', 'Μαρτίου': '03',
    'Απρίλιος': '04', 'Απριλίου': '04',
    'Μάιος': '05', 'Μαΐου': '05', 'Μαίου': '05',
    'Ιούνιος': '06', 'Ιουνίου': '06',
    'Ιούλιος': '07', 'Ιουλίου': '07',
    'Αύγουστος': '08', 'Αυγούστου': '08',
    'Σεπτέμβριος': '09', 'Σεπτεμβρίου': '09',
    'Οκτώβριος': '10', 'Οκτωβρίου': '10',
    'Νοέμβριος': '11', 'Νοεμβρίου': '11',
    'Δεκέμβριος': '12', 'Δεκεμβρίου': '12',
}

_BACKOFF = (1, 3, 9)


class CivilprotectionGovGrDeltia(BaseCrawler):
    site_id = "civilprotection-gov-gr-deltia-tupou"
    site_name = "Custom: civilprotection-gov-gr-deltia-tupou"
    base_url = "https://civilprotection.gov.gr"

    _LIST_BASE = "https://civilprotection.gov.gr/deltia-tupou"
    _LIST_QS = (
        "filter_content_type%5B%5D=deltio_typoy"
        "&field_omilia_value%5B%5D=2"
        "&field_omilia_value_op=not"
    )
    _PUBLISHER = "Ministry of Climate Crisis and Civil Protection, Greece"
    _MIN_ABSTRACT = 50
    _MIN_SAVE_ABSTRACT = 100
    _MAX_PAGES = 200
    _BUDGET_SECS = 25 * 60

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        if _CFFI_AVAILABLE:
            self._cffi = _cffi_requests.Session(impersonate="chrome120")
        else:
            self._cffi = None

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        page = 0
        t0 = time.monotonic()

        while True:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - t0 > self._BUDGET_SECS:
                print(f"[{self.site_id}] 25-min budget reached; exiting cleanly")
                break
            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap {self._MAX_PAGES} pages reached; stopping")
                break

            list_url = self._list_url(page)
            raw = self._get(list_url, referer=self.base_url + "/")
            if not raw:
                print(f"[{self.site_id}] list fetch failed at page {page}; stopping")
                break

            soup = self._make_soup(raw, context=f"list page {page}")
            if soup is None:
                print(f"[{self.site_id}] list parse failed at page {page}; stopping")
                break

            rows = soup.select(".views-row")
            if not rows:
                print(f"[{self.site_id}] no rows at page {page}; done")
                break

            new_this_page = 0
            for idx, row in enumerate(rows, start=1):
                if limit is not None and saved >= limit:
                    break
                label = f"p{page}.r{idx}"
                try:
                    saved, new_this_page = self._process_row(
                        row, label, list_url, seen_urls, saved, new_this_page, limit
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {label} failed: {exc}")
                    continue

            if page > 0 and page % 10 == 0:
                lim_s = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_s}")

            if new_this_page == 0:
                print(f"[{self.site_id}] page {page}: no new items; stopping")
                break

            if not self._has_next_page(soup, page):
                print(f"[{self.site_id}] no next page after page {page}; done")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    def _process_row(self, row, label, list_url, seen_urls, saved, new_this_page, limit):
        a_tag = row.select_one("a[href]")
        if not a_tag:
            return saved, new_this_page

        href = a_tag.get("href", "").strip()
        if not href:
            return saved, new_this_page

        url = urljoin(self.base_url, href)
        if url in seen_urls:
            return saved, new_this_page
        seen_urls.add(url)
        new_this_page += 1

        # Extract list-level signals
        title_el = row.select_one(".simple-post__title")
        list_title = self._one_line(title_el.get_text(" ", strip=True) if title_el else "")
        time_el = row.select_one("time[datetime]")
        list_date = self._parse_iso_date(time_el.get("datetime", "") if time_el else "")
        cat_el = row.select_one(".simple-post-cat")
        list_cat = self._one_line(cat_el.get_text(" ", strip=True) if cat_el else "Δελτίο Τύπου")

        time.sleep(self._delay)
        detail_raw = self._get(url, referer=list_url)
        if not detail_raw:
            raise RuntimeError("detail fetch failed after retries")

        detail_soup = self._make_soup(detail_raw, context=f"item {label} detail")
        if detail_soup is None:
            raise RuntimeError("detail HTML parse failed")

        parsed = self._parse_detail(detail_soup, url, list_title, list_date, list_cat)
        abstract = parsed.get("abstract", "")

        if len(abstract) < self._MIN_ABSTRACT:
            print(
                f"[{self.site_id}] item {label} skipped: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return saved, new_this_page
        if len(abstract) < self._MIN_SAVE_ABSTRACT:
            print(
                f"[{self.site_id}] item {label} skipped: "
                f"abstract below save threshold ({len(abstract)} chars)"
            )
            return saved, new_this_page

        self._save_paper({
            "id": None,
            "site_id": self.site_id,
            "external_id": parsed["external_id"],
            "post_number": parsed["post_number"],
            "title": parsed["title"],
            "abstract": abstract,
            "published_date": parsed["published_date"],
            "listed_date": list_date,
            "authors": "",
            "publisher": self._PUBLISHER,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": parsed["pdf_url"],
            "keywords": parsed["keywords"],
            "category": parsed["category"],
            "doi": None,
            "original_filename": parsed["original_filename"],
            "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
        })
        saved += 1
        lim_s = f"/{limit}" if limit is not None else ""
        print(f"[{self.site_id}] saved {saved}{lim_s}: {parsed['title'][:80]}")

        return saved, new_this_page

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, soup, url, fallback_title, fallback_date, fallback_cat):
        dt_header = soup.select_one(".dt-header")

        # --- Title ---
        title = ""
        if dt_header:
            h1 = dt_header.select_one("h1")
            if h1:
                span = h1.select_one("span")
                title = self._one_line(
                    span.get_text(" ", strip=True) if span else h1.get_text(" ", strip=True)
                )
        if not title:
            # fallback: second h1 in document
            h1s = soup.find_all("h1")
            if len(h1s) >= 2:
                title = self._one_line(h1s[1].get_text(" ", strip=True))
        if not title:
            title = fallback_title

        # --- Category ---
        category = fallback_cat
        if dt_header:
            cat_el = dt_header.select_one(".post-category span")
            if cat_el:
                category = self._one_line(cat_el.get_text(" ", strip=True))

        # --- Published date ---
        published_date = fallback_date
        if dt_header:
            date_el = dt_header.select_one(".post-date")
            if date_el:
                published_date = (
                    self._parse_greek_date(date_el.get_text(strip=True)) or fallback_date
                )

        # --- Node ID (post_number) ---
        node_id = ""
        shortlink = soup.select_one("link[rel=shortlink]")
        if shortlink:
            m = re.search(r"/node/(\d+)", shortlink.get("href", ""))
            if m:
                node_id = m.group(1)
        external_id = node_id or self._slug_from_url(url)
        post_number = node_id if node_id else None

        # --- PDF URL (extract before decomposing dt-content) ---
        pdf_url = ""
        original_filename = None
        dt_content = soup.select_one(".dt-content")
        if dt_content:
            for a in dt_content.select("a[href]"):
                href = a.get("href", "")
                if re.search(r"\.pdf(?:[?#]|$)", href, re.I):
                    pdf_url = urljoin(self.base_url, href)
                    path_part = urlparse(pdf_url).path
                    fname = path_part.split("/")[-1]
                    if fname:
                        original_filename = fname
                    break

        # --- Abstract (body text from .dt-content) ---
        abstract = ""
        if dt_content:
            # Work on a clone-like copy by re-parsing just this fragment
            frag = self._make_soup(str(dt_content), context="dt-content fragment")
            if frag:
                # Remove PDF card, images, share widgets, captions
                for bad in frag.select(
                    ".info-card, .dt-img-span, .lezanta-fotografias, "
                    ".img-preview, .clearfix, .a2a_kit, script, style, img"
                ):
                    bad.decompose()
                parts = []
                for node in frag.find_all(["p", "li", "h2", "h3", "h4"]):
                    t = self._one_line(node.get_text(" ", strip=True))
                    if t and t not in parts:
                        parts.append(t)
                if parts:
                    abstract = "\n\n".join(parts)
                else:
                    abstract = self._one_line(frag.get_text(" ", strip=True))

        # --- Keywords from meta ---
        keywords = ""
        meta_kw = soup.find("meta", attrs={"name": "keywords"})
        if meta_kw and meta_kw.get("content"):
            # Site keywords are site-wide, not article-specific; use category
            keywords = category

        metadata: dict = {
            "node_id": node_id,
            "posted_date": published_date,
            "category": category,
            "source": "civilprotection.gov.gr HTML detail page",
        }
        if original_filename:
            metadata["originalFilename"] = original_filename

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "keywords": keywords,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": category,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _get(self, url: str, referer: str = None, retries: int = 3) -> str | None:
        """Fetch URL using curl_cffi (Chrome120 TLS) to bypass Akamai WAF."""
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "el-GR,el;q=0.9,en-US;q=0.8,en;q=0.7",
        }
        if referer:
            headers["Referer"] = referer

        last_err = ""
        for attempt in range(1, retries + 1):
            try:
                if self._cffi is not None:
                    r = self._cffi.get(url, headers=headers, timeout=45)
                    if r.status_code >= 400:
                        raise RuntimeError(f"HTTP {r.status_code}")
                    body = r.text
                else:
                    r = self._session.get(url, headers=headers, timeout=45)
                    r.raise_for_status()
                    body = r.content.decode("utf-8", errors="replace")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                last_err = str(exc)
                print(
                    f"[{self.site_id}] fetch attempt {attempt}/{retries} "
                    f"for {url}: {last_err}"
                )
                if attempt < retries:
                    wait = _BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] fetch failed after {retries} attempts for {url}: {last_err}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML") -> BeautifulSoup | None:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed "
                    f"for {context}: {exc}"
                )
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    def _list_url(self, page: int) -> str:
        base = f"{self._LIST_BASE}?{self._LIST_QS}"
        if page == 0:
            return base
        return f"{base}&page={page}%2C0"

    def _has_next_page(self, soup: BeautifulSoup, current_page: int) -> bool:
        """Return True if there's a 'next' link in the Drupal pager."""
        next_link = soup.select_one(".cp-pagination-item.next[href]")
        if next_link:
            href = next_link.get("href", "")
            return bool(href and href != "javascript:void(0)")
        # Fallback: look for numeric link to next page
        next_num = current_page + 1
        for a in soup.select(".cp-pagination a[href]"):
            href = a.get("href", "")
            if f"page={next_num}%2C0" in href or f"page={next_num},0" in href:
                return True
        return False

    # ------------------------------------------------------------------
    # Date utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_iso_date(dt_str: str) -> str:
        if not dt_str:
            return ""
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", dt_str.strip())
        return m.group(1) if m else ""

    @classmethod
    def _parse_greek_date(cls, text: str) -> str:
        if not text:
            return ""
        text = text.strip()
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
        for month_name, month_num in _GREEK_MONTHS.items():
            if month_name in text:
                day_m = re.search(r"\b(\d{1,2})\b", text)
                year_m = re.search(r"\b(\d{4})\b", text)
                if day_m and year_m:
                    day = day_m.group(1).zfill(2)
                    year = year_m.group(1)
                    return f"{year}-{month_num}-{day}"
        return ""

    # ------------------------------------------------------------------
    # Text utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _one_line(text) -> str:
        return re.sub(r"\s+", " ", unescape(str(text or "")).replace("\xa0", " ")).strip()

    @staticmethod
    def _slug_from_url(url: str) -> str:
        path = urlparse(url).path
        parts = [p for p in path.split("/") if p]
        return parts[-1] if parts else url
