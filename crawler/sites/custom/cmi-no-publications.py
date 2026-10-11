# -*- coding: utf-8 -*-
"""CMI (Chr. Michelsen Institute) journal-article publications crawler.

Starting URL : https://www.cmi.no/publications/search?pubtype=journal-articles
Pagination   : https://www.cmi.no/publications/search?page={N}&pubtype=journal-articles
               (20 items/page; requests beyond the last page may repeat it)
Detail page  : https://www.cmi.no/publications/{numeric-id}-{slug}
"""

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import date
from urllib.parse import parse_qs, urlsplit

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.cmi.no"

_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _curl_get(url, retries=3):
    """Fetch URL via curl with retries (1s, 3s, 9s backoff). Returns decoded text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[cmi-no-publications] curl error (attempt {attempt + 1}/{retries}) for {url}: {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt
            time.sleep(wait)
    return None


def _make_soup(html):
    """Parse HTML with html5lib -> lxml -> html.parser fallback chain."""
    if not html:
        return None
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _publication_date(raw):
    """Preserve header date precision and forthcoming status separately."""
    text = re.sub(r"\s+", " ", raw or "").strip().rsplit("|", 1)[-1].strip().lower()
    status = "forthcoming" if re.search(r"\bforthcoming\b", text) else None
    if status:
        text = re.sub(r"^forthcoming\s*", "", text).strip()
    if not text:
        return None, "missing", None, status
    if re.fullmatch(r"\d{4}", text) and 1 <= int(text) <= 9999:
        return None, "year", text, status
    match = re.fullmatch(r"(?:(\d{1,2})\s+)?([a-z]+)\.?\s+(\d{4})", text)
    if not match:
        return None, "unparsed", None, status
    day, name, year = match.groups()
    months = dict(_MONTH_MAP)
    months.update({name: f"{i:02d}" for i, name in enumerate(
        ("january", "february", "march", "april", "may", "june", "july", "august",
         "september", "october", "november", "december"), 1)})
    month = months.get(name)
    if month is None or not 1 <= int(year) <= 9999:
        return None, "invalid", None, status
    if day is None:
        return None, "month", f"{year}-{month}", status
    try:
        value = date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None, "invalid", None, status
    return None if status else value, "day", value, status


def _parse_month_year(text):
    """Return a publication day only when the source supplies one."""
    return _publication_date(text)[0]


class CmiNoPublicationsCrawler(BaseCrawler):
    """Crawler for CMI (Chr. Michelsen Institute) journal-article publications."""

    site_id = "cmi-no-publications"
    site_name = "Custom: cmi-no-publications"
    base_url = "https://www.cmi.no"

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        url = f"{_BASE}/publications/search?page={page}&pubtype=journal-articles"
        raw = _curl_get(url)
        return _make_soup(raw)

    def _extract_list_items(self, soup):
        """Return [(detail_url, external_id), ...] from a listing page."""
        items = []
        if soup is None:
            return items
        for div in soup.find_all("div", class_="list-item publication"):
            h3 = div.find("h3", class_="pub-title")
            if not h3:
                continue
            a = h3.find("a", href=True)
            if not a:
                continue
            href = a["href"]
            if not href.startswith("/publications/"):
                continue
            full_url = _BASE + href
            m = re.match(r"^/publications/(\d+)-", href)
            ext_id = m.group(1) if m else href.rstrip("/").split("/")[-1]
            items.append((full_url, ext_id))
        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_detail(self, url, ext_id):
        """Fetch and parse one publication detail page.

        Returns a paper dict suitable for _save_paper(), or None on failure.
        """
        raw = _curl_get(url)
        if not raw:
            return None
        soup = _make_soup(raw)
        if soup is None:
            return None

        header = soup.find("div", class_="page-header")
        if header is None:
            return None

        h1 = header.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else ""
        title = re.sub(r"\s+", " ", title).strip()
        if not title:
            return None

        doi = None
        if h1:
            a = h1.find("a", href=True)
            if a and "doi.org" in a["href"]:
                doi = a["href"]
        if not doi:
            m_doi = re.search(r'href=["\'](https?://doi\.org/[^"\']+)["\']', raw, re.I)
            if m_doi:
                doi = m_doi.group(1)

        pubdate_div = header.find("div", class_="publishdate")
        pubdate_raw = pubdate_div.get_text(" ", strip=True) if pubdate_div else ""
        published_date, date_precision, date_period, publication_status = _publication_date(pubdate_raw)

        author_div = header.find("div", class_="publication-author")
        authors = None
        if author_div:
            links = [a.get_text(strip=True) for a in author_div.find_all("a") if a.get_text(strip=True)]
            if links:
                authors = "; ".join(links)
            else:
                text = author_div.get_text(" ", strip=True)
                text = re.sub(r"\s*\(\d{4}\)\s*$", "", text).strip()
                authors = text or None

        ref_div = header.find("div", class_="publication-ref")
        journal_raw = ref_div.get_text(" ", strip=True) if ref_div else ""
        journal = re.sub(r"^in\s+", "", journal_raw, flags=re.I).strip() or None
        volume = None
        m_vol = re.search(r"vol\.\s*(\S+)", journal_raw, re.I)
        if m_vol:
            volume = m_vol.group(1).rstrip(",.")

        abstract = ""
        text_div = soup.find("div", class_="publication-text")
        if text_div:
            # Embedded publisher pages can contain scripts and styles. html5lib
            # exposes their contents through get_text(), unlike other parsers.
            for element in text_div.find_all(["script", "style"]):
                element.decompose()
            abstract = text_div.get_text(" ", strip=True)
        abstract = re.sub(r"\s+", " ", abstract).strip()

        pdf_url = None
        pdf_a = soup.find("a", class_="publication-pdf")
        if pdf_a and pdf_a.get("href"):
            pdf_href = pdf_a["href"].strip()
            pdf_url = pdf_href if pdf_href.startswith("http") else _BASE + pdf_href

        original_filename = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]

        metadata = {
            "publication_date_raw": pubdate_raw,
            "publication_date_precision": date_precision,
            "publication_period": date_period,
            "publication_status": publication_status,
            "publication_date_role": "journal_publication_header",
            "source_abstract_state": "present" if abstract else "empty",
            "journal_raw": journal_raw or None,
            "node_id": ext_id,
        }
        if volume:
            metadata["volume"] = volume
        if original_filename:
            metadata["originalFilename"] = original_filename

        return {
            "site_id": self.site_id,
            "external_id": ext_id,
            "post_number": ext_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": None,
            "listed_date": None,
            "authors": authors,
            "publisher": "Chr. Michelsen Institute",
            "department": None,
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": "Journal Article",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Crawl
    # ------------------------------------------------------------------

    def _source_page(self, soup, requested_page):
        if soup is None:
            self._report_source_failure("fetch_failed")
            raise ValueError("missing CMI source list")
        pager = soup.select_one("div.pagination")
        active = pager.select("a.active[href]") if pager else []
        if len(active) != 1:
            raise ValueError("CMI source pagination is missing or ambiguous")
        pages = []
        for anchor in pager.select("a[href]"):
            parts = urlsplit(anchor["href"])
            query = parse_qs(parts.query)
            if (parts.path != "/publications/search"
                    or parts.netloc not in ("", "www.cmi.no")
                    or parts.scheme not in ("", "https")
                    or set(query) != {"page", "pubtype"}
                    or query["pubtype"] != ["journal-articles"]
                    or len(query["page"]) != 1
                    or not query["page"][0].isdigit()):
                raise ValueError("CMI source pagination changed scope")
            number = int(query["page"][0])
            if number < 1:
                raise ValueError("invalid CMI source page number")
            pages.append(number)
            if anchor is active[0] and number != requested_page:
                raise ValueError("CMI source page clamped or did not advance")
        items = self._extract_list_items(soup)
        if (not items or len({u for u, _ in items}) != len(items)
                or len({key for _, key in items}) != len(items)):
            raise ValueError("empty or ambiguous CMI source list")
        return items, max(pages)

    def crawl(self, limit=None):
        self._exhausted = False
        self._source_failure = None
        if limit is not None and limit <= 0:
            return 0
        self._check_cancelled()
        cursor = dict(self.delivery_cursor or {})
        scope = cursor.get("scope", "journal_articles")
        page = cursor.get("page", 1)
        index = cursor.get("next_index", 0)
        last_page = cursor.get("last_page", 0)
        expected_digest = cursor.get("list_digest", "")
        if (cursor.get("version", 1) != 1 or scope not in ("journal_articles", "done")
                or type(page) is not int or page < 1
                or type(index) is not int or index < 0
                or type(last_page) is not int or last_page < 0
                or not isinstance(expected_digest, str)
                or (index and not re.fullmatch(r"[a-f0-9]{64}", expected_digest))
                or (scope == "done" and (index or expected_digest or last_page != page))):
            raise ValueError("invalid CMI source cursor")
        saved = 0
        started = time.monotonic()
        budget = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        while scope != "done":
            self._check_cancelled()
            if (limit is not None and saved >= limit) or time.monotonic() - started >= budget:
                return saved
            try:
                soup = self._fetch_list_page(page)
                self._check_cancelled()
                items, observed_last = self._source_page(soup, page)
                digest = hashlib.sha256(json.dumps(items).encode()).hexdigest()
                if ((expected_digest and digest != expected_digest) or index > len(items)
                        or (last_page and observed_last != last_page)):
                    raise ValueError("CMI source list changed since checkpoint; overlap review required")
                last_page = observed_last
                for offset in range(index, len(items)):
                    self._check_cancelled()
                    if (limit is not None and saved >= limit) or time.monotonic() - started >= budget:
                        return saved
                    detail_url, ext_id = items[offset]
                    time.sleep(self._delay)
                    self._check_cancelled()
                    paper = self._parse_detail(detail_url, ext_id)
                    self._check_cancelled()
                    if paper is None:
                        self._report_source_failure("item_failed")
                        raise ValueError("CMI source detail could not be parsed")
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                    self._advance_cursor(dict(version=1, scope=scope, page=page,
                                              next_index=offset + 1, last_page=last_page,
                                              list_digest=digest), items_done=1)
            except Exception:
                self._report_source_failure("invalid_response")
                raise
            scope = "done" if page == last_page else "journal_articles"
            if scope != "done":
                page += 1
            index, expected_digest = 0, ""
            self._advance_cursor(dict(version=1, scope=scope, page=page,
                                      next_index=0, last_page=last_page, list_digest=""))
        self._check_cancelled()
        self._mark_exhausted()
        return saved
