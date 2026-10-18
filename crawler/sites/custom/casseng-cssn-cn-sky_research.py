# -*- coding: utf-8 -*-
"""Crawler for CASS English journals publication list.

Target: http://casseng.cssn.cn/sky_research/publications/journals/
Structure: Single listing page → 8 journal profile detail pages (.shtml).
Each detail page describes one CASS-sponsored academic journal (title,
supervisor, sponsor, publisher, ISSN, etc.). No JS-rendered pagination;
the entire list is on one HTML page.
"""

import json
import os
import re
import subprocess
import time
import uuid
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_LIST_URL = "http://casseng.cssn.cn/sky_research/publications/journals/"
_BASE    = "http://casseng.cssn.cn"


class CassengCssncnSkyResearchCrawler(BaseCrawler):
    site_id   = "casseng-cssn-cn-sky_research"
    site_name = "Custom: casseng-cssn-cn-sky_research"
    base_url  = "http://casseng.cssn.cn"

    _MAX_PAGES          = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECONDS        = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _BUDGET_MARGIN_S    = 60
    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = (1, 3, 9)
        last_err = ""
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5, check=False)
                raw = r.stdout.decode("utf-8", errors="replace")
                if r.returncode == 0 and raw.strip():
                    return raw
                last_err = (r.stderr or b"").decode("utf-8", errors="replace").strip() or f"exit {r.returncode}"
            except Exception as exc:
                last_err = str(exc)
            if attempt < 2:
                w = waits[attempt]
                print(f"[{self.site_id}] curl failed {url} (attempt {attempt+1}/3): {last_err}; retry in {w}s")
                time.sleep(w)
        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_err}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(text):
        if text is None:
            return ""
        t = unescape(str(text)).replace("\xa0", " ").replace("　", " ")
        return re.sub(r"\s+", " ", t).strip()

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed: {exc}")
        try:
            return BeautifulSoup("", "html.parser")
        except Exception:
            return None

    def _meta(self, soup, name):
        tag = soup.find("meta", attrs={"name": name}) if soup else None
        return self._clean(tag.get("content", "") if tag else "")

    # ------------------------------------------------------------------
    # Listing page
    # ------------------------------------------------------------------

    def _fetch_list(self, page=0):
        """Read one source-declared journal page without changing item fields."""
        self._check_cancelled()
        page_url = _LIST_URL if page == 0 else urljoin(_LIST_URL, f"index_{page}.shtml")
        raw = self._curl_get(page_url)
        self._check_cancelled()
        if not raw:
            raise RuntimeError("journal listing fetch failed")
        count = re.search(r"var countPage\s*=\s*(\d+)", raw)
        current = re.search(r"var currentPage\s*=\s*(\d+)", raw)
        if not count or not current or int(current.group(1)) != page or int(count.group(1)) <= page:
            raise ValueError("invalid source journal pagination")
        if page == 0:
            self._source_page_count = int(count.group(1))
        elif int(count.group(1)) != self._source_page_count:
            raise ValueError("journal source page count changed during traversal")
        soup = self._make_soup(raw)
        if soup is None:
            raise ValueError("journal listing HTML could not be parsed")
        results = self._list_rows(soup, page_url)

        if not results:
            raise ValueError("source-declared journal page has no entries")
        return results

    def _list_rows(self, soup, page_url):
        """Journal rows of one parsed listing page (no I/O)."""
        results = []
        # Journal entries are inside <ul class="cont3-1text cont3-2text">
        ul = soup.find("ul", class_=re.compile(r"cont3-1text"))
        if ul is None:
            # Fallback: any link inside .cont3-text ending in .shtml
            container = soup.find(class_="cont3-text") or soup
            links = container.find_all("a", href=re.compile(r"\.shtml$"))
        else:
            links = ul.find_all("a", href=re.compile(r"\.shtml$"))

        for a in links:
            href = self._clean(a.get("href", ""))
            if not href or href.lower().startswith(("javascript:", "mailto:", "#")):
                continue
            title = self._clean(a.get("title") or a.get_text(" ", strip=True))
            abs_url = urljoin(page_url, href)
            if abs_url and title:
                results.append({"title": title, "url": abs_url})
        return results

    def _iter_list_pages(self, should_stop=lambda: False):
        page = 0
        while page == 0 or page < self._source_page_count:
            self._check_cancelled()
            if should_stop():
                return
            yield page, self._fetch_list(page)
            page += 1

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _parse_content_fields(self, soup):
        """Extract structured fields from the journal-description content div."""
        content_div = soup.select_one(".about-content.news-detail, .about2-content")
        if content_div is None:
            content_div = soup.select_one(".about-content")
        fields = {}
        if content_div is None:
            return fields, ""

        # Build full abstract text from the div
        full_text = self._clean(content_div.get_text(" ", strip=True))
        fields["_full_text"] = full_text

        # Extract key→value pairs from <strong>Label:</strong> Value patterns
        label_map = {}
        for strong in content_div.find_all("strong"):
            label = self._clean(strong.get_text(" ", strip=True)).rstrip(":").strip()
            if not label:
                continue
            # Collect following text up to next <strong> or <br>
            parts = []
            for sib in strong.next_siblings:
                sib_str = str(sib)
                if sib.name == "strong" if hasattr(sib, "name") else False:
                    break
                if hasattr(sib, "get_text"):
                    t = self._clean(sib.get_text(" ", strip=True))
                else:
                    t = self._clean(str(sib))
                if t:
                    parts.append(t)
                # Stop at <br> after getting a non-empty chunk
                if hasattr(sib, "name") and sib.name == "br" and parts:
                    break
            value = self._clean(" ".join(parts))
            if label and value:
                label_map[label.lower()] = value

        fields["label_map"] = label_map
        return fields, full_text

    def _parse_detail(self, list_row, detail_url, raw):
        soup = self._make_soup(raw)
        if soup is None:
            raise ValueError("failed to parse detail HTML with all parsers")

        # --- IDs ---
        content_id = self._meta(soup, "contentid")
        if not content_id:
            # fall back to data-article_id attribute
            parent = soup.select_one(".qt_article_parent")
            content_id = self._clean(parent.get("data-article_id", "")) if parent else ""
        if not content_id:
            # last resort: extract from URL path segment like t20230714_5668150
            m = re.search(r"_(\d+)\.shtml", detail_url)
            content_id = m.group(1) if m else detail_url.rstrip("/").split("/")[-1]

        external_id = content_id

        # --- Dates ---
        published_date = self._meta(soup, "publishdate")
        if not published_date:
            dt_span = soup.select_one(".qt_published_date")
            published_date = self._clean(dt_span.get_text(" ", strip=True)) if dt_span else ""
        # Normalise to YYYY-MM-DD
        dm = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", published_date)
        if dm:
            published_date = f"{int(dm.group(1)):04d}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"

        listed_date = published_date  # single date on this site

        # --- Title ---
        h2 = soup.select_one(".qt_article_title")
        title = self._clean(h2.get_text(" ", strip=True)) if h2 else self._clean(list_row.get("title", ""))
        if not title:
            t_tag = soup.find("title")
            title = self._clean(t_tag.get_text(" ", strip=True)).split(" - ")[0] if t_tag else ""

        # --- Content / abstract ---
        content_fields, full_text = self._parse_content_fields(soup)
        label_map = content_fields.get("label_map", {})

        # Build rich abstract: prefer full text (it includes all structured fields)
        abstract = full_text

        # --- Publisher ---
        publisher = ""
        for k in ("publisher", "sponsor"):
            publisher = label_map.get(k, "")
            if publisher:
                break

        # --- Authors / editors ---
        authors_raw = ""
        for k in ("editor-in-chief", "editor", "deputy editor-in-chief"):
            authors_raw = label_map.get(k, "")
            if authors_raw:
                break
        if not authors_raw:
            authors_raw = self._meta(soup, "author")

        # Normalise: split on common separators → join with ;
        if authors_raw:
            parts = re.split(r"[;；,，]+", authors_raw)
            authors = "; ".join(p for p in (self._clean(p) for p in parts) if p)
        else:
            authors = ""

        # --- ISSN as keyword ---
        issn = label_map.get("issn", "")
        keywords = issn if issn else ""

        # --- Category / journal ---
        category = "Journals"
        journal = title  # each record IS a journal profile

        # --- PDF (none on this site) ---
        pdf_url = None
        original_filename = None

        # --- DOI ---
        doi = ""
        doi_m = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", full_text)
        if doi_m:
            doi = doi_m.group(0).rstrip(".,;")

        # --- metadata ---
        metadata = {
            "contentid": content_id,
            "catalogs": self._meta(soup, "catalogs"),
            "source": self._meta(soup, "source"),
            "label_map": label_map,
            "list_url": _LIST_URL,
            "detail_url": detail_url,
        }

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.site_id}:{external_id}")),
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "journal": journal,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        start_time = time.monotonic()
        saved = 0
        seen_urls = set()
        def should_stop():
            if limit is not None and saved >= limit:
                return True
            if time.monotonic() - start_time >= self._MAX_SECONDS - self._BUDGET_MARGIN_S:
                raise TimeoutError("journal traversal time budget reached before completion")
            return False
        for page, rows in self._iter_list_pages(should_stop):
            for row in rows:
                self._check_cancelled()
                if should_stop():
                    return saved
                detail_url = self._clean(row.get("url", ""))
                if not detail_url:
                    raise ValueError("journal entry is missing its detail URL")
                if detail_url in seen_urls:
                    raise ValueError("source journal URL repeated across pages")
                seen_urls.add(detail_url)
                if self.detail_delay:
                    time.sleep(self.detail_delay)
                self._check_cancelled()
                raw_detail = self._curl_get(detail_url, referer=_LIST_URL)
                self._check_cancelled()
                if not raw_detail:
                    raise RuntimeError("journal detail fetch failed")
                paper = self._parse_detail(row, detail_url, raw_detail)
                if not paper.get("title"):
                    raise ValueError("journal detail has no title")
                abstract = self._clean(paper.get("abstract", ""))
                if len(abstract) < self._MIN_ABSTRACT_CHARS:
                    print(f"[{self.site_id}] item {detail_url} skipped: short abstract ({len(abstract)} chars)")
                    continue
                paper["abstract"] = abstract
                self._save_paper(paper)
                if self._last_save_created:
                    saved += 1
        return saved

    # ------------------------------------------------------------------
    # Enumeration split (crawl() unchanged; same parsing and Paper fields)
    # ------------------------------------------------------------------

    def _enumeration_items(self, raw, url):
        """Rows of one listing page (journals/ = page 0, index_N.shtml = page N).

        The page's own countPage/currentPage must agree with the requested page, as in
        crawl(); anything else is a listing parse error, never an empty end page.
        """
        from crawler.enumeration.http import PermanentFailure
        m = re.search(r"/index_(\d+)\.shtml$", url or "")
        page = int(m.group(1)) if m else 0
        count = re.search(r"var countPage\s*=\s*(\d+)", raw or "")
        current = re.search(r"var currentPage\s*=\s*(\d+)", raw or "")
        if not count or not current or int(current.group(1)) != page or int(count.group(1)) <= page:
            raise PermanentFailure("listing_parse_error:invalid_source_pagination", url)
        soup = self._make_soup(raw)
        rows = self._list_rows(soup, url) if soup is not None else []
        if not rows:
            raise PermanentFailure("listing_parse_error:declared_page_without_entries", url)
        return rows

    def _enumeration_detail(self, raw, item, url):
        """crawl()'s Paper for one journal; None where crawl skips (short abstract)."""
        detail_url = self._clean(item.get("url", ""))
        if not detail_url:
            raise ValueError("journal entry is missing its detail URL")
        paper = self._parse_detail(item, detail_url, raw)
        if not paper.get("title"):
            raise ValueError("journal detail has no title")
        abstract = self._clean(paper.get("abstract", ""))
        if len(abstract) < self._MIN_ABSTRACT_CHARS:
            return None
        paper["abstract"] = abstract
        return paper
