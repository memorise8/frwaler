# -*- coding: utf-8 -*-
"""BMWET Publikationen crawler — https://www.bmwet.gv.at/Services/Publikationen.html

The index page is a single HTML page with collapsible category sections.
Each section lists publications that link to either:
  - Detail pages at /Services/Publikationen/...html  (rich abstract)
  - Direct PDF files at /dam/jcr:UUID/filename.pdf   (no abstract → skipped)
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

_SITE_ID = "bmwet-gv-at-services"
_BASE_URL = "https://www.bmwet.gv.at"
_INDEX_URL = "https://www.bmwet.gv.at/Services/Publikationen.html"
_ABSTRACT_MIN_CHARS = 50
_MAX_SAFETY_ITEMS = 10_000  # safety cap instead of pages (no pagination on this site)
_MAX_WALL_SECONDS = 25 * 60

_DE_MONTHS = {
    "januar": "01", "februar": "02", "märz": "03", "maerz": "03",
    "april": "04", "mai": "05", "juni": "06", "juli": "07",
    "august": "08", "september": "09", "oktober": "10",
    "november": "11", "dezember": "12",
}


class BmwetGvAtServicesCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: bmwet-gv-at-services"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        """Fetch URL via curl with 3 retries and exponential backoff."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: de-AT,de;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 15
                )
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                print(f"[{_SITE_ID}] Empty response attempt {attempt} for {url}")
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error attempt {attempt}: {exc}")
            if attempt < len(waits):
                time.sleep(wait)
        print(f"[{_SITE_ID}] All retries exhausted for {url}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """Parse HTML trying html5lib, lxml, html.parser in order."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _text(element):
        """Clean text from a BS4 element."""
        if element is None:
            return ""
        t = element.get_text(separator=" ")
        return re.sub(r"\s+", " ", t).strip()

    @staticmethod
    def _parse_german_date(text):
        """Extract ISO date from German text (best-effort).

        Tries full date '19. Februar 2025' → '2025-02-19', then year-only.
        """
        m = re.search(
            r"(\d{1,2})\.\s+([A-ZÄÖÜa-zäöüß]+)\s+(\d{4})", text
        )
        if m:
            day, month_name, year = m.groups()
            month = _DE_MONTHS.get(month_name.lower())
            if month:
                return f"{year}-{month}-{day.zfill(2)}"
        m = re.search(r"\b(20\d{2})\b", text)
        if m:
            return m.group(1)
        return None

    # ------------------------------------------------------------------
    # Index page parsing
    # ------------------------------------------------------------------

    def _parse_index_page(self, html):
        """Return list of entry dicts from the publications index page.

        Each entry: {category, title, url, url_type}
        url_type: 'detail' | 'pdf' | 'external' | None
        """
        soup = self._make_soup(html)
        if soup is None:
            print(f"[{_SITE_ID}] Failed to parse index page HTML")
            return []

        content = soup.find(id="content")
        if content is None:
            content = soup.find("main") or soup

        entries = []
        current_category = "Publikationen"

        for section in content.find_all("div", class_="card-collapse"):
            # Extract category from H3 heading
            h3 = section.find(["h3", "H3"])
            if h3:
                btn = h3.find("button") or h3
                raw_cat = self._text(btn)
                # Remove trailing icon text (non-letter chars after last word)
                raw_cat = re.sub(r"\s+[^\w].*$", "", raw_cat, flags=re.DOTALL)
                raw_cat = raw_cat.strip()
                if raw_cat:
                    current_category = raw_cat

            # Process all list items in this section
            for li in section.find_all("li"):
                a = li.find("a")
                if a is None:
                    # Text-only entries — no URL, skip
                    continue

                href = a.get("href", "").strip()
                # Link text; strip "(PDF, X KB)" info spans
                for span in a.find_all("span", class_="fileinfo"):
                    span.decompose()
                title = self._text(a)
                # Also clean up icon-only spans
                title = re.sub(r"\s+", " ", title).strip()

                if not href or not title:
                    continue

                # Classify URL type
                if (href.startswith("/Services/Publikationen/")
                        and href.endswith(".html")
                        and href not in ("/Services/Publikationen.html",
                                         "/Services/Publikationen/veroeffentlichungen-gem-art-20-abs-5-b-vg.html",
                                         "/Services/Publikationen/medientransparenz.html")):
                    url_type = "detail"
                    full_url = _BASE_URL + href
                elif ".pdf" in href.lower() or "/dam/" in href:
                    url_type = "pdf"
                    full_url = (
                        _BASE_URL + href if href.startswith("/") else href
                    )
                elif href.startswith("http"):
                    url_type = "external"
                    full_url = href
                else:
                    url_type = "other"
                    full_url = (
                        _BASE_URL + href if href.startswith("/") else href
                    )

                entries.append({
                    "category": current_category,
                    "title": title,
                    "url": full_url,
                    "url_type": url_type,
                })

        return entries

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail_page(self, html, page_url):
        """Extract metadata from a publication detail page.

        Returns dict: title, abstract, pdf_url, pdf_filename, authors,
                      publisher, published_date
        """
        soup = self._make_soup(html)
        if soup is None:
            return {}

        content = soup.find(id="content")
        if content is None:
            content = soup.find("main") or soup

        # Title
        title = ""
        h1 = content.find("h1")
        if h1:
            span = h1.find("span", class_="title")
            title = self._text(span if span else h1)

        # Abstract: collect paragraph text before any H2 section
        abstract_parts = []
        for elem in content.children:
            if not hasattr(elem, "name") or elem.name is None:
                continue
            if elem.name in ("h2", "h3", "H2", "H3"):
                break
            if elem.name == "p":
                t = self._text(elem)
                if t:
                    abstract_parts.append(t)
            elif elem.name in ("ul", "ol"):
                for li in elem.find_all("li"):
                    t = self._text(li)
                    if t:
                        abstract_parts.append(t)

        abstract = "\n\n".join(abstract_parts)

        # Authors/publisher from bold labels in first paragraph
        authors = None
        publisher = None
        first_p = content.find("p")
        if first_p:
            for strong in first_p.find_all(["strong", "b"]):
                label = strong.get_text().strip().rstrip(":").lower()
                # Get sibling text (everything after the bold tag until next tag)
                value_parts = []
                for sib in strong.next_siblings:
                    if hasattr(sib, "name") and sib.name in ("strong", "b", "br"):
                        break
                    sib_text = (
                        sib.get_text() if hasattr(sib, "get_text")
                        else str(sib)
                    )
                    sib_text = re.sub(r"\s+", " ", sib_text).strip()
                    if sib_text:
                        value_parts.append(sib_text)
                value = " ".join(value_parts).strip().rstrip(";,")
                if not value:
                    continue
                if "autor" in label:
                    authors = value
                elif "auftragnehmer" in label or "auftraggeber" in label:
                    publisher = value

        # Fallback: scan abstract text for patterns
        if not authors:
            m = re.search(
                r"Autoren?[:\s]+([^\n]+?)(?:\s*(?:Auftragnehmer|\Z))",
                abstract,
            )
            if m:
                authors = m.group(1).strip().rstrip(",;")

        # PDF link — first PDF/dam link in the page
        pdf_url = None
        pdf_filename = None
        for a in content.find_all("a"):
            href = a.get("href", "")
            if ".pdf" in href.lower() or "/dam/" in href:
                if href.startswith("/"):
                    pdf_url = _BASE_URL + href
                elif href.startswith("http"):
                    pdf_url = href
                else:
                    pdf_url = urljoin(page_url, href)
                fname = unquote(urlparse(pdf_url).path.split("/")[-1])
                if fname.lower().endswith(".pdf"):
                    pdf_filename = fname
                break

        published_date = self._parse_german_date(abstract) if abstract else None

        return {
            "title": title,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "pdf_filename": pdf_filename,
            "authors": authors,
            "publisher": publisher,
            "published_date": published_date or "",
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl BMWET publications index and save items.

        The index is a single HTML page — no server-side pagination.
        We iterate through all discovered entries respecting `limit`.
        """
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        print(f"[{_SITE_ID}] Starting crawl (limit={limit_str})")

        # Fetch index page
        index_html = self._curl(_INDEX_URL, referer=_BASE_URL)
        if not index_html:
            print(f"[{_SITE_ID}] Failed to fetch index page, aborting.")
            return 0

        entries = self._parse_index_page(index_html)
        print(f"[{_SITE_ID}] Parsed {len(entries)} entries from index page")

        processed = 0
        for idx, entry in enumerate(entries):
            # Time budget check
            elapsed = time.time() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(
                    f"[{_SITE_ID}] Wall-clock budget ({_MAX_WALL_SECONDS}s) "
                    f"reached after {idx} items. Stopping."
                )
                break

            # Limit check
            if limit is not None and saved >= limit:
                break

            # Safety item cap
            if idx >= _MAX_SAFETY_ITEMS:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_SAFETY_ITEMS} items reached.")
                break

            url = entry["url"]
            url_type = entry["url_type"]
            category = entry["category"]
            index_title = entry["title"]

            # Only detail pages have rich abstracts
            if url_type != "detail":
                continue

            if url in seen_urls:
                print(f"[{_SITE_ID}] Duplicate URL, skipping: {url}")
                continue
            seen_urls.add(url)

            # Per-item failure isolation
            try:
                time.sleep(self._detail_delay)

                detail_html = self._curl(url, referer=_INDEX_URL)
                if not detail_html:
                    print(f"[{_SITE_ID}] item {idx} failed: no response from {url}")
                    continue

                detail = self._parse_detail_page(detail_html, url)
                item_title = detail.get("title") or index_title
                if not item_title:
                    print(f"[{_SITE_ID}] item {idx}: no title, skipping ({url})")
                    continue

                abstract = detail.get("abstract", "")

                if len(abstract) < _ABSTRACT_MIN_CHARS:
                    print(
                        f"[{_SITE_ID}] item {idx}: abstract too short "
                        f"({len(abstract)} chars < {_ABSTRACT_MIN_CHARS}), "
                        f"skipping: {item_title[:50]}"
                    )
                    continue

                # external_id: URL path slug (unique per publication)
                path = urlparse(url).path
                slug = path.rstrip("/").split("/")[-1].replace(".html", "")
                # Include parent path segment for uniqueness across categories
                path_parts = [p for p in path.split("/") if p]
                if len(path_parts) >= 2:
                    slug = f"{path_parts[-2]}__{path_parts[-1].replace('.html', '')}"

                pdf_url = detail.get("pdf_url") or ""
                pdf_filename = detail.get("pdf_filename") or ""
                authors = detail.get("authors") or ""
                publisher = (
                    detail.get("publisher")
                    or "Bundesministerium für Wirtschaft, Energie und Tourismus"
                )

                paper = {
                    "id": None,
                    "site_id": _SITE_ID,
                    "external_id": slug,
                    "post_number": slug,
                    "title": item_title,
                    "abstract": abstract,
                    "url": url,
                    "pdf_url": pdf_url,
                    "authors": authors,
                    "publisher": publisher,
                    "department": "",
                    "journal": "",
                    "category": category,
                    "keywords": "",
                    "published_date": detail.get("published_date") or "",
                    "listed_date": "",
                    "doi": "",
                    "original_filename": pdf_filename,
                    "metadata": json.dumps(
                        {
                            "posted_date": None,
                            "originalFilename": pdf_filename or None,
                            "category": category,
                            "index_title": index_title,
                            "source_url": url,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                counter = f"{saved}/{limit}" if limit is not None else str(saved)
                print(f"[{_SITE_ID}] Saved {counter}: {item_title[:70]}")

                processed += 1
                if processed % 10 == 0:
                    print(
                        f"[{_SITE_ID}] page {processed // 10}: "
                        f"saved {saved}/{limit_str}"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
