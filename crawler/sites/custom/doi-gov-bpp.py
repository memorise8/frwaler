# -*- coding: utf-8 -*-
"""Crawler for DOI Budget Planning & Performance – Budget in Briefs.

Target: https://www.doi.gov/bpp/budget-briefs
Each fiscal year page lists individual chapter PDFs (Departmental Highlights,
Bureau Highlights, Appendixes) plus a whole-book download. We save each PDF
as its own document record.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler


_SITE_ID = "doi-gov-bpp"
_BASE_URL = "https://www.doi.gov"
_INDEX_URL = "https://www.doi.gov/bpp/budget-briefs"
_PUBLISHER = "U.S. Department of the Interior"
_FALLBACK_DESC = (
    "This page highlights items of interest concerning the Department of the "
    "Interior's budget. You may track the progress of this budget request "
    "through the Congressional appropriations process from this page."
)
_MAX_WALL_SECONDS = 25 * 60  # 25 minutes
_MAX_PAGES = 200              # safety cap (index links, not paginator pages)


# ---------------------------------------------------------------------------
# BeautifulSoup helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Try parsers in order: html5lib → lxml → html.parser. Return soup or None."""
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


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class DOIGovBPPCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: doi-gov-bpp"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Budget in Briefs index and each fiscal-year detail page.

        Saves one record per PDF file found. Returns number of records saved.
        """
        start_time = time.time()
        seen_urls: set[str] = set()
        saved = 0
        limit_str = str(limit) if limit is not None else "∞"

        print(f"[doi-gov-bpp] Starting crawl (limit={limit_str})")

        # 1. Fetch & parse index page
        index_html = self._curl_get(_INDEX_URL)
        if not index_html:
            print("[doi-gov-bpp] FATAL: could not fetch index page")
            return 0

        year_entries = self._parse_index(index_html)
        print(f"[doi-gov-bpp] Found {len(year_entries)} fiscal-year pages on index")

        page_num = 0
        for year_url, year_label in year_entries:
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print("[doi-gov-bpp] Wall-clock budget reached; exiting cleanly")
                break
            if page_num >= _MAX_PAGES:
                print(f"[doi-gov-bpp] Safety cap of {_MAX_PAGES} pages reached")
                break

            page_num += 1
            if page_num % 10 == 0:
                print(f"[doi-gov-bpp] page {page_num}: saved {saved}/{limit_str}")

            try:
                full_url = (_BASE_URL + year_url) if year_url.startswith("/") else year_url
                year_html = self._curl_get(full_url)
                if not year_html:
                    print(f"[doi-gov-bpp] Skipping {year_url}: fetch failed")
                    continue

                records = self._parse_year_page(year_html, full_url, year_label)
                print(f"[doi-gov-bpp] {year_label}: {len(records)} PDF records")

                for rec in records:
                    if limit is not None and saved >= limit:
                        break

                    # URL-based deduplication
                    dedup_key = rec.get("pdf_url") or rec.get("url", "")
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    # Skip short abstracts (log but do not crash)
                    abstract = rec.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[doi-gov-bpp] skip short abstract: "
                            f"{rec.get('title', '')[:60]}"
                        )
                        continue

                    try:
                        self._save_paper(rec)
                        saved += 1
                    except Exception as exc:
                        print(
                            f"[doi-gov-bpp] save failed for "
                            f"{rec.get('title', '')[:60]}: {exc}"
                        )

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[doi-gov-bpp] Error processing {year_url}: {exc}")

        print(f"[doi-gov-bpp] Crawl complete. Saved {saved} records.")
        return saved

    # ------------------------------------------------------------------
    # Private — network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """HTTP GET via curl with SSL workaround and retry/backoff."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "-L",
                        "-A", self.USER_AGENT,
                        "--max-time", "30",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                if result.returncode == 0 and result.stdout:
                    try:
                        return result.stdout.decode("utf-8", errors="replace")
                    except Exception:
                        return result.stdout.decode("latin-1", errors="replace")
                print(
                    f"[doi-gov-bpp] curl returned empty/error for {url} "
                    f"(attempt {attempt + 1}/{retries})"
                )
            except subprocess.TimeoutExpired:
                print(f"[doi-gov-bpp] curl timeout for {url} (attempt {attempt + 1})")
            except Exception as exc:
                print(f"[doi-gov-bpp] curl exception for {url}: {exc}")

            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                print(f"[doi-gov-bpp] Retrying in {wait}s…")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Private — parsing
    # ------------------------------------------------------------------

    def _parse_index(self, html: str) -> list[tuple[str, str]]:
        """Return [(relative_url, label)] for each FY highlights page."""
        soup = _make_soup(html)
        if soup is None:
            return []
        results: list[tuple[str, str]] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href: str = a["href"]
            if re.match(r"^/budget/appropriations/\d{4}/highlights", href):
                if href not in seen:
                    seen.add(href)
                    label = a.get_text(strip=True) or href
                    results.append((href, label))
        return results

    def _parse_year_page(
        self, html: str, page_url: str, year_label: str
    ) -> list[dict]:
        """Return list of paper-dict records for every PDF on the year page."""
        soup = _make_soup(html)
        if soup is None:
            return []

        # ---- Page-level metadata ----
        h1 = soup.find("h1")
        page_title = h1.get_text(strip=True) if h1 else year_label

        meta_desc = _FALLBACK_DESC
        tag = soup.find("meta", attrs={"name": "description"})
        if tag and tag.get("content"):
            meta_desc = unescape(tag["content"].strip()) or _FALLBACK_DESC

        node_id: str | None = None
        shortlink = soup.find("link", rel="shortlink")
        if shortlink and shortlink.get("href"):
            m = re.search(r"/node/(\d+)", shortlink["href"])
            if m:
                node_id = m.group(1)

        pub_date = self._derive_pub_date(soup, page_url)

        fy_year: str | None = None
        m2 = re.search(r"/(\d{4})/highlights", page_url)
        if m2:
            fy_year = m2.group(1)

        common = {
            "page_title": page_title,
            "meta_desc": meta_desc,
            "node_id": node_id,
            "pub_date": pub_date,
            "fy_year": fy_year,
            "page_url": page_url,
        }

        # ---- Try newer layout (separate paragraph blocks with <h2>) ----
        paragraphs = soup.find_all(
            "div", class_=re.compile(r"paragraph--type--content-formatter-text")
        )
        if paragraphs:
            return self._extract_from_paragraphs(paragraphs, common)

        # ---- Fallback: older single body block (FY2020-style) ----
        body_div = soup.find("div", class_=re.compile(r"field--name-body"))
        if body_div:
            return self._extract_from_body(body_div, common)

        return []

    # ------------------------------------------------------------------

    def _extract_from_paragraphs(
        self, paragraphs, common: dict
    ) -> list[dict]:
        records = []
        for para in paragraphs:
            section_h2 = para.find("h2")
            section_name = section_h2.get_text(strip=True) if section_h2 else ""
            for a in para.find_all("a", href=True):
                rec = self._link_to_record(a, section_name, common)
                if rec is not None:
                    records.append(rec)
        return records

    def _extract_from_body(self, body_div, common: dict) -> list[dict]:
        """Walk body div, tracking section headings from h2/h3 tags."""
        records = []
        current_section = ""
        # descendants gives us every tag in document order
        for elem in body_div.descendants:
            if not hasattr(elem, "name") or elem.name is None:
                continue
            if elem.name in ("h2", "h3"):
                text = elem.get_text(strip=True)
                if "download" not in text.lower():
                    current_section = text
            elif elem.name == "a" and elem.get("href", "").endswith(".pdf"):
                rec = self._link_to_record(elem, current_section, common)
                if rec is not None:
                    records.append(rec)
        return records

    # ------------------------------------------------------------------

    def _link_to_record(self, a_tag, section_name: str, common: dict) -> dict | None:
        """Convert an <a href=…pdf> tag into a paper dict, or None if skipped."""
        href: str = a_tag.get("href", "")
        if not href.endswith(".pdf"):
            return None

        pdf_url = (_BASE_URL + href) if href.startswith("/") else href
        link_title = unescape((a_tag.get("title") or "").strip())
        link_text = a_tag.get_text(strip=True)
        uuid = a_tag.get("data-entity-uuid", "")

        # Collect nested sub-bullet text (TOC topics inside the chapter entry)
        sub_items: list[str] = []
        parent_li = a_tag.find_parent("li")
        if parent_li:
            nested_ul = parent_li.find("ul")
            if nested_ul:
                for li2 in nested_ul.find_all("li", recursive=False):
                    sub_items.append(li2.get_text(strip=True))

        display_title = link_title or link_text
        if not display_title:
            display_title = href.rsplit("/", 1)[-1]

        # Construct full record title: prefer link title, else combine
        full_title = link_title or f"{common['page_title']} – {link_text}"

        abstract = self._build_abstract(
            display_title, section_name, sub_items,
            common["meta_desc"], common["page_title"]
        )
        if len(abstract) < 50:
            return None

        external_id = uuid if uuid else self._derive_ext_id(href)
        filename = href.rsplit("/", 1)[-1] if "/" in href else href

        # Derive date from PDF path if it contains YYYY-MM pattern
        pub_date = common["pub_date"]
        path_date = re.search(r"/(\d{4}-\d{2})/", href)
        if path_date and pub_date is None:
            pub_date = path_date.group(1) + "-01"

        fy_year = common["fy_year"]
        return {
            "site_id": _SITE_ID,
            "external_id": external_id,
            "post_number": None,
            "title": full_title,
            "abstract": abstract,
            "url": common["page_url"],
            "pdf_url": pdf_url,
            "published_date": pub_date,
            "listed_date": pub_date,
            "authors": None,
            "publisher": _PUBLISHER,
            "department": section_name or None,
            "journal": None,
            "category": "Budget in Brief",
            "keywords": ",".join(
                filter(None, ["budget", "interior", "fiscal year", fy_year or ""])
            ),
            "doi": None,
            "original_filename": filename,
            "metadata": json.dumps(
                {
                    "posted_date": pub_date,
                    "originalFilename": filename,
                    "node_id": common["node_id"],
                    "fy_year": fy_year,
                    "section": section_name,
                    "page_title": common["page_title"],
                    "link_text": link_text,
                    "sub_items": sub_items,
                    "entity_uuid": uuid,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------

    def _build_abstract(
        self,
        doc_title: str,
        section_name: str,
        sub_items: list[str],
        meta_desc: str,
        page_title: str,
    ) -> str:
        """Build a ≥100-char abstract from available metadata."""
        parts: list[str] = []
        if doc_title:
            parts.append(doc_title + ".")
        if section_name:
            parts.append(f"Section: {section_name}.")
        if sub_items:
            parts.append("Covers: " + "; ".join(sub_items[:8]) + ".")

        base = " ".join(parts)

        if len(base) < 100:
            supplement = (
                f" Part of the {page_title}. Published by the "
                f"U.S. Department of the Interior. {meta_desc}"
            )
            base = (base + supplement).strip()

        return base[:3000]

    # ------------------------------------------------------------------

    @staticmethod
    def _derive_pub_date(soup, page_url: str) -> str | None:
        """Best-effort extraction of a publication date."""
        # 1. dcterms.date meta tag
        tag = soup.find("meta", attrs={"name": "dcterms.date"})
        if tag and tag.get("content"):
            return tag["content"].strip()
        # 2. Derive from FY year (files published roughly a year before FY start)
        m = re.search(r"/(\d{4})/highlights", page_url)
        if m:
            fy = int(m.group(1))
            return f"{fy - 1}-01-01"
        return None

    @staticmethod
    def _derive_ext_id(href: str) -> str:
        """Derive a stable external_id from a PDF href path."""
        filename = href.rsplit("/", 1)[-1]
        return re.sub(r"[^a-zA-Z0-9_-]", "_", filename)
