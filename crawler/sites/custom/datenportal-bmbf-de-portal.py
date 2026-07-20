# -*- coding: utf-8 -*-
"""Datenportal des BMFTR (formerly BMBF) statistics-table crawler.

Starting URL: https://www.datenportal.bmbf.de/portal/de/bufi.html
(redirects to the live domain https://www.datenportal.bmftr.bund.de/portal/de/).

This is a government statistics portal, not an article/paper repository: its
content unit is a numbered data table ("Tab 1.1.1", "Tab 2.5.83", ...) grouped
under two chapters — K1 "Forschung und Innovation" and K2 "Hochschulen". Each
table has an HTML detail page with a source ("Quelle") block, footnotes
("Anmerkungen") and a last-updated date, plus a directly downloadable PDF.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []

_GERMAN_MONTHS = {
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "april": 4, "mai": 5,
    "juni": 6, "juli": 7, "august": 8, "september": 9, "oktober": 10,
    "november": 11, "dezember": 12,
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


def _parse_german_date(text: str):
    """Parse 'Letzte Aktualisierung: 27. März 2026' style text to ISO date."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})\.\s*([A-Za-zÄÖÜäöüß]+)\s*(\d{4})", text)
    if m:
        day, month_name, year = m.groups()
        month = _GERMAN_MONTHS.get(month_name.strip().lower())
        if month:
            return f"{int(year):04d}-{month:02d}-{int(day):02d}"
    m = re.search(r"([A-Za-zÄÖÜäöüß]+)\s*(\d{4})", text)
    if m:
        month_name, year = m.groups()
        month = _GERMAN_MONTHS.get(month_name.strip().lower())
        if month:
            return f"{int(year):04d}-{month:02d}-01"
    m = re.search(r"(\d{4})", text)
    if m:
        return f"{m.group(1)}-01-01"
    return None


class DatenportalBmbfDePortalCrawler(BaseCrawler):
    """Crawler for the BMFTR/BMBF Datenportal statistics tables."""

    site_id = "datenportal-bmbf-de-portal"
    site_name = "Custom: datenportal-bmbf-de-portal"
    base_url = "https://www.datenportal.bmbf.de"

    _LIVE_BASE = "https://www.datenportal.bmftr.bund.de/portal/de"
    _CHAPTERS = [
        ("K1", "Forschung und Innovation"),
        ("K2", "Hochschulen"),
    ]
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str):
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
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
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_chapter(self, raw_html: str, chapter_code: str, chapter_title: str):
        """Parse all table teasers from a chapter list page (K1.html / K2.html)."""
        items = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for chapter {chapter_code}: {exc}")
            return items

        for art in soup.find_all("article", attrs={"data-igr-nr": True}):
            try:
                table_number = (art.get("data-igr-nr") or "").strip()
                if not table_number:
                    continue

                h3s = art.find_all("h3")
                title = h3s[1].get_text(strip=True) if len(h3s) > 1 else (
                    h3s[0].get_text(strip=True) if h3s else ""
                )
                if not title:
                    continue

                zeitreihe = ""
                p_tags = art.find_all("p")
                if p_tags:
                    zr_text = p_tags[0].get_text(" ", strip=True)
                    m = re.search(r"Zeitreihe:\s*(.+)", zr_text)
                    zeitreihe = m.group(1).strip() if m else zr_text

                html_span = art.find("span", class_="ft_HTML")
                pdf_span = art.find("span", class_="ft_PDF")
                html_link = html_span.find("a") if html_span else None
                pdf_link = pdf_span.find("a") if pdf_span else None
                if not html_link or not html_link.get("href"):
                    continue
                detail_url = html_link["href"].strip()
                pdf_url = (pdf_link.get("href") or "").strip() if pdf_link else ""
                if pdf_url and pdf_url.startswith("/"):
                    pdf_url = "https://www.datenportal.bmftr.bund.de" + pdf_url

                items.append({
                    "table_number": table_number,
                    "title": title,
                    "zeitreihe": zeitreihe,
                    "url": detail_url,
                    "pdf_url": pdf_url or None,
                    "chapter_code": chapter_code,
                    "chapter_title": chapter_title,
                    "year": art.get("data-igr-year") or "",
                })
            except Exception as exc:
                print(f"[{self.site_id}] item parse error (chapter {chapter_code}): {exc}")
                continue

        return items

    def _parse_detail(self, raw_html: str):
        """Extract source / notes / last-updated / license text from a table detail page."""
        result = {"quelle": "", "notes": "", "updated_raw": "", "updated_iso": None, "license": ""}
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for detail page: {exc}")
            return result

        src = soup.select_one("#source")
        if src:
            result["quelle"] = src.get_text(" ", strip=True)

        notes = soup.select_one("#notes")
        if notes:
            result["notes"] = notes.get_text(" ", strip=True)

        upd = soup.select_one("#upd")
        if upd:
            result["updated_raw"] = upd.get_text(" ", strip=True)
            result["updated_iso"] = _parse_german_date(result["updated_raw"])

        lic = soup.select_one("#license")
        if lic:
            result["license"] = lic.get_text(" ", strip=True)

        return result

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
        return tail or None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl BMFTR Datenportal statistics tables across both chapters (K1, K2)."""
        saved = 0
        seen_urls = set()
        start_time = time.time()
        page_num = 0
        lim_str = str(limit) if limit is not None else "inf"

        all_items = []
        try:
            for chapter_code, chapter_title in self._CHAPTERS:
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded during listing. Stopping cleanly.")
                    break

                page_num += 1
                chapter_url = f"{self._LIVE_BASE}/{chapter_code}.html"
                raw = self._curl_get(chapter_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch chapter {chapter_code}. Skipping.")
                    continue

                items = self._parse_chapter(raw, chapter_code, chapter_title)
                new_count = 0
                for item in items:
                    if item["url"] in seen_urls:
                        continue
                    seen_urls.add(item["url"])
                    all_items.append(item)
                    new_count += 1

                print(
                    f"[{self.site_id}] chapter {chapter_code}: "
                    f"{len(items)} tables found, {new_count} new"
                )

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

            print(f"[{self.site_id}] Total unique tables discovered: {len(all_items)}")

            for idx, item in enumerate(all_items, start=1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                try:
                    time.sleep(self._delay)
                    raw_detail = self._curl_get(item["url"])
                    if not raw_detail:
                        print(f"[{self.site_id}] Failed to fetch detail for {item['table_number']}. Skipping.")
                        continue

                    detail = self._parse_detail(raw_detail)

                    parts = [item["title"]]
                    if item["zeitreihe"]:
                        parts.append(f"Zeitreihe: {item['zeitreihe']}.")
                    if detail["quelle"]:
                        parts.append(f"Quelle: {detail['quelle']}")
                    if detail["notes"]:
                        parts.append(f"Anmerkungen: {detail['notes']}")
                    abstract = " ".join(p for p in parts if p).strip()

                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Short abstract ({len(abstract)}) for "
                            f"'{item['title'][:50]}', skipping."
                        )
                        continue

                    published_date = detail["updated_iso"] or (
                        f"{item['year']}-01-01" if item["year"] else None
                    )
                    listed_date = published_date

                    publisher = None
                    if detail["quelle"]:
                        try:
                            src_soup = _make_soup(raw_detail).select_one("#source")
                            anchors = src_soup.find_all("a") if src_soup else []
                            names = []
                            for a in anchors:
                                name = a.get_text(strip=True)
                                if name and name not in names:
                                    names.append(name)
                            publisher = names if names else None
                        except Exception:
                            publisher = None

                    original_filename = self._filename_from_url(item["pdf_url"])

                    meta = {
                        "posted_date": detail["updated_raw"] or None,
                        "originalFilename": original_filename,
                        "table_number": item["table_number"],
                        "chapter_code": item["chapter_code"],
                        "chapter_title": item["chapter_title"],
                        "zeitreihe": item["zeitreihe"] or None,
                        "quelle_raw": detail["quelle"] or None,
                        "notes_raw": detail["notes"] or None,
                        "license": detail["license"] or None,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item["table_number"],
                        "post_number": item["table_number"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": None,
                        "publisher": publisher,
                        "department": None,
                        "journal": None,
                        "url": item["url"],
                        "pdf_url": item["pdf_url"],
                        "keywords": item["chapter_title"],
                        "category": item["chapter_title"],
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {item['title'][:60]}")

                    if idx % 10 == 0:
                        print(f"[{self.site_id}] page {idx}: saved {saved}/{lim_str}")

                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {item.get('table_number', '?')} failed: {exc}; continuing."
                    )
                    continue

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
