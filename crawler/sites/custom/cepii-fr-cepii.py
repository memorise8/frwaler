# -*- coding: utf-8 -*-
"""CEPII (cepii.fr) research reports crawler.

Starting URL: https://www.cepii.fr/CEPII/fr/publications/reports.asp?offset=10
List pages are paginated via ``offset`` (10 items/page); once the offset
passes the last real page, the site clamps and keeps re-serving the final
page's items — detected here via URL dedup against ``seen_urls``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    from bs4 import NavigableString as _NavigableString
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _NavigableString = None
    _PARSERS = []

_MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11,
    "décembre": 12, "decembre": 12,
}


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _split_by_br(fragment_html: str) -> list:
    """Split a small HTML fragment into text groups separated by <br/> tags."""
    if not fragment_html or _BS is None:
        return []
    soup = _make_soup(fragment_html)
    body = soup.body if getattr(soup, "body", None) else soup
    groups = [[]]
    for node in getattr(body, "contents", []):
        if getattr(node, "name", None) == "br":
            groups.append([])
        else:
            groups[-1].append(node)
    out = []
    for group in groups:
        parts = []
        for node in group:
            if isinstance(node, _NavigableString):
                parts.append(str(node))
            else:
                parts.append(node.get_text())
        text = _clean_text("".join(parts))
        if text:
            out.append(text)
    return out


def _parse_fr_date(raw: str) -> str:
    """Parse a French 'mois AAAA' string (e.g. 'décembre 2012') to ISO YYYY-MM-01."""
    if not raw:
        return ""
    m = re.search(r"([A-Za-zéûôàêç]+)\s+(\d{4})", raw)
    if not m:
        y = re.search(r"(\d{4})", raw)
        return f"{y.group(1)}-01-01" if y else ""
    month_name = m.group(1).lower()
    year = int(m.group(2))
    month = _MONTHS_FR.get(month_name, 1)
    return f"{year:04d}-{month:02d}-01"


class CepiiFrCepiiCrawler(BaseCrawler):
    """Crawler for CEPII research reports (cepii.fr)."""

    site_id = "cepii-fr-cepii"
    site_name = "Custom: cepii-fr-cepii"
    base_url = "https://www.cepii.fr"

    _LIST_PATH = "/CEPII/fr/publications/reports.asp"
    _PAGE_SIZE = 10
    _MIN_ABSTRACT = 100
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str:
        """GET via curl with exponential-backoff retries. Returns decoded text or ''."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return ""

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw_html: str) -> list:
        """Return [{'noDoc': str, 'url': str, 'title': str}, ...] from a list page."""
        entries = []
        if not raw_html:
            return entries
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] list page parse error: {exc}")
            return entries

        for span in soup.find_all("span", class_="titre-actu"):
            try:
                link = span.find("a", href=True)
                if not link:
                    continue
                href = link["href"].strip()
                m = re.search(r"NoDoc=(\d+)", href)
                if not m:
                    continue
                no_doc = m.group(1)
                title = _clean_text(link.get_text())
                url = urljoin(self.base_url, href)
                entries.append({"noDoc": no_doc, "url": url, "title": title})
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
                continue
        return entries

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail_page(self, raw_html: str, no_doc: str) -> dict:
        """Parse a detail page into a field dict, or {} on unrecoverable failure."""
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] detail page parse error (NoDoc={no_doc}): {exc}")
            return {}

        container = soup.find("div", class_="colonnepage530px")
        if container is None:
            return {}

        titre_div = container.find("div", class_="titre-page")
        title = ""
        authors = []
        if titre_div is not None:
            parts = re.split(r"<br\s*/?>\s*<br\s*/?>", str(titre_div), flags=re.I)
            if parts:
                try:
                    title = _clean_text(_make_soup(parts[0]).get_text(" "))
                except Exception:
                    title = ""
            if len(parts) > 1:
                authors = _split_by_br(parts[1])

        # Abstract: text between the title block and the "Mots-clés" marker.
        container_html = str(container)
        abstract = ""
        title_close = re.search(r'<div class="titre-page">.*?</div>', container_html, re.S)
        rest = container_html[title_close.end():] if title_close else container_html
        cut = re.search(r"Mots-cl", rest)
        if cut:
            rest = rest[:cut.start()]
        try:
            abstract = _clean_text(_make_soup(rest).get_text(" "))
        except Exception as exc:
            print(f"[{self.site_id}] abstract parse error (NoDoc={no_doc}): {exc}")
            abstract = ""

        # Citation (report number + raw date).
        citation_span = soup.find("span", class_="citation")
        citation_text = _clean_text(citation_span.get_text(" ")) if citation_span else ""
        report_no = ""
        date_raw = ""
        cm = re.search(r"N°\s*([^,]+),\s*(.+)$", citation_text)
        if cm:
            report_no = cm.group(1).strip()
            date_raw = cm.group(2).strip()

        # Keywords / JEL.
        kw_spans = soup.find_all("span", class_="titre-ss-rubrique")
        keywords_raw = _clean_text(kw_spans[0].get_text(" ")) if kw_spans else ""
        jel_raw = _clean_text(kw_spans[1].get_text(" ")) if len(kw_spans) > 1 else ""
        keywords = ", ".join(p.strip() for p in re.split(r"\s*\|\s*", keywords_raw) if p.strip())

        # PDF.
        pdf_link = soup.find("a", href=re.compile(r"/PDF_PUB/"))
        pdf_url = urljoin(self.base_url, pdf_link["href"]) if pdf_link else None

        # Themes / category.
        themes_div = soup.find("div", class_="ss_menu_droit_themes")
        category = ""
        if themes_div is not None:
            theme_span = themes_div.find("span", class_="themes")
            if theme_span is not None:
                category = _clean_text(theme_span.get_text(" "))

        return {
            "title": title,
            "authors": authors,
            "abstract": abstract,
            "report_no": report_no,
            "date_raw": date_raw,
            "keywords": keywords,
            "jel": jel_raw,
            "pdf_url": pdf_url,
            "category": category,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl CEPII research reports, walking ?offset= pages until exhausted."""
        if _BS is None:
            print(f"[{self.site_id}] BeautifulSoup unavailable; aborting.")
            return 0

        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        offset = 0
        page_num = 0
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_num += 1
                list_url = f"{self.base_url}{self._LIST_PATH}?offset={offset}"
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch offset={offset}. Stopping.")
                    break

                entries = self._parse_list_page(raw)
                if not entries:
                    print(f"[{self.site_id}] No entries at offset={offset}. Stopping.")
                    break

                new_entries = [e for e in entries if e["url"] not in seen_urls]
                if not new_entries:
                    print(f"[{self.site_id}] Page at offset={offset} had no new items (end of list). Stopping.")
                    break

                for entry in new_entries:
                    if limit is not None and saved >= limit:
                        break

                    seen_urls.add(entry["url"])
                    no_doc = entry["noDoc"]

                    try:
                        time.sleep(self._delay)
                        detail_raw = self._curl_get(entry["url"])
                        if not detail_raw:
                            print(f"[{self.site_id}] item NoDoc={no_doc} failed: no detail response; skipping.")
                            continue

                        detail = self._parse_detail_page(detail_raw, no_doc)
                        if not detail:
                            print(f"[{self.site_id}] item NoDoc={no_doc} failed: could not parse detail page; skipping.")
                            continue

                        title = detail.get("title") or entry.get("title") or ""
                        if not title:
                            print(f"[{self.site_id}] item NoDoc={no_doc} has empty title; skipping.")
                            continue

                        abstract = detail.get("abstract") or ""
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] item NoDoc={no_doc} abstract too short "
                                f"({len(abstract)} chars); skipping."
                            )
                            continue

                        published_date = _parse_fr_date(detail.get("date_raw", "")) or None
                        listed_date = published_date

                        authors = "; ".join(detail.get("authors") or [])
                        pdf_url = detail.get("pdf_url")
                        original_filename = None
                        if pdf_url:
                            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                            if "." in tail:
                                original_filename = tail

                        meta = {
                            "posted_date": detail.get("date_raw") or "",
                            "node_id": no_doc,
                            "report_number": detail.get("report_no") or "",
                            "series": "CEPII Research Report",
                        }
                        if original_filename:
                            meta["originalFilename"] = original_filename
                        if detail.get("jel"):
                            meta["jel_codes"] = detail["jel"]

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": no_doc,
                            "post_number": no_doc,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": authors,
                            "publisher": "CEPII",
                            "department": None,
                            "journal": None,
                            "url": entry["url"],
                            "pdf_url": pdf_url,
                            "keywords": detail.get("keywords") or "",
                            "category": detail.get("category") or "",
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item NoDoc={no_doc} failed: {exc}; continuing.")
                        continue

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

                offset += self._PAGE_SIZE

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
