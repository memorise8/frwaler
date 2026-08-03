# -*- coding: utf-8 -*-
"""CMI (Chr. Michelsen Institute) journal-article publications crawler.

Starting URL : https://www.cmi.no/publications/search?pubtype=journal-articles
Pagination   : https://www.cmi.no/publications/search?page={N}&pubtype=journal-articles
               (20 items/page; end reached when a page has 0 items)
Detail page  : https://www.cmi.no/publications/{numeric-id}-{slug}
"""

import json
import os
import re
import subprocess
import time

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


def _parse_month_year(text):
    """Parse 'Jun 2026' (or similar) -> 'YYYY-MM-01'."""
    if not text:
        return None
    m = re.search(r"([A-Za-z]{3,9})\.?\s+(\d{4})", text)
    if not m:
        return None
    mon_key = m.group(1)[:3].lower()
    mm = _MONTH_MAP.get(mon_key)
    if not mm:
        return None
    return f"{m.group(2)}-{mm}-01"


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
        published_date = _parse_month_year(pubdate_raw)

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
            "posted_date": pubdate_raw,
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
            "posted_date": published_date,
            "listed_date": published_date,
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

    def crawl(self, limit=None):
        start_time = time.time()
        budget_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        safety_page_cap = 200

        saved = 0
        page = 1
        seen_urls = set()
        limit_display = limit if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - start_time
                if elapsed > budget_seconds:
                    print(f"[cmi-no-publications] time budget ({budget_seconds}s) exceeded at page {page}, stopping")
                    break

                if page > safety_page_cap:
                    print(f"[cmi-no-publications] reached safety cap of {safety_page_cap} pages, stopping")
                    break

                soup = self._fetch_list_page(page)
                if soup is None:
                    print(f"[cmi-no-publications] page {page}: failed to fetch/parse, stopping")
                    break

                list_items = self._extract_list_items(soup)
                if not list_items:
                    print(f"[cmi-no-publications] page {page}: 0 items, stopping (end of pagination)")
                    break

                new_items = [(u, eid) for (u, eid) in list_items if u not in seen_urls]
                if not new_items:
                    print(f"[cmi-no-publications] page {page}: all items already seen, stopping (paginator loop)")
                    break

                for detail_url, ext_id in new_items:
                    if limit is not None and saved >= limit:
                        break

                    elapsed = time.time() - start_time
                    if elapsed > budget_seconds:
                        print(f"[cmi-no-publications] time budget ({budget_seconds}s) exceeded, stopping")
                        break

                    seen_urls.add(detail_url)

                    try:
                        time.sleep(self._delay)
                        paper = self._parse_detail(detail_url, ext_id)
                        if paper is None:
                            print(f"[cmi-no-publications] item {ext_id} ({detail_url}): failed to parse, skipping")
                            continue
                        if not paper.get("abstract") or len(paper["abstract"]) < 50:
                            print(f"[cmi-no-publications] item {ext_id} ({detail_url}): abstract too short, skipping")
                            continue
                        self._save_paper(paper)
                        saved += 1
                    except Exception as exc:
                        print(f"[cmi-no-publications] item {ext_id} ({detail_url}) failed: {exc}")
                        continue

                if page % 10 == 0:
                    print(f"[cmi-no-publications] page {page}: saved {saved}/{limit_display}")

                page += 1
        except KeyboardInterrupt:
            print(f"[cmi-no-publications] interrupted by user at page {page}, saved {saved}")
            raise

        print(f"[cmi-no-publications] done: saved {saved}/{limit_display} after {page - 1} pages")
        return saved
