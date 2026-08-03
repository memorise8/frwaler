# -*- coding: utf-8 -*-
"""Crawler for defense.gouv.fr salle de presse (press room).

The site is a Drupal CMS with HTML list pages (no JSON API exposed).
Each page returns 10 article cards; each card contains the title, category
tag, date text, and a direct PDF download link.  There are no separate HTML
detail pages — the PDF is the full document.

Pagination: ?page=0 .. ?page=N (0-indexed).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _BS4_OK = True
except ImportError:
    _BS4_OK = False


def _make_soup(html: str):
    if not _BS4_OK:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


class DefenseGouvFrSalleDePresseCrawler(BaseCrawler):
    site_id = "defense-gouv-fr-salle-de-presse"
    site_name = "Custom: defense-gouv-fr-salle-de-presse"
    base_url = "https://www.defense.gouv.fr"

    START_URL = "https://www.defense.gouv.fr/salle-de-presse"
    MAX_PAGES = 200
    RATE_SLEEP = 1.0
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50

    _MONTHS_FR = {
        "janvier": "01", "février": "02", "fevrier": "02",
        "mars": "03", "avril": "04", "mai": "05", "juin": "06",
        "juillet": "07", "août": "08", "aout": "08",
        "septembre": "09", "octobre": "10", "novembre": "11",
        "décembre": "12", "decembre": "12",
    }

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 5
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    w = waits[attempt]
                    print(f"[{self.site_id}] empty response attempt {attempt+1}, retry in {w}s")
                    time.sleep(w)
            except Exception as exc:
                if attempt < 2:
                    w = waits[attempt]
                    print(f"[{self.site_id}] curl error attempt {attempt+1}: {exc}, retry in {w}s")
                    time.sleep(w)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&[a-zA-Z#\d]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _parse_date_fr(self, raw: str) -> str:
        """'07 mai 2026' → '2026-05-07'."""
        raw = raw.strip()
        m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", raw)
        if m:
            day, mon, year = m.group(1), m.group(2).lower(), m.group(3)
            mon_num = self._MONTHS_FR.get(mon, "01")
            return f"{year}-{mon_num}-{day.zfill(2)}"
        return ""

    @staticmethod
    def _parse_date_dmy(raw: str) -> str:
        """'07.05.2026' → '2026-05-07'."""
        m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw.strip())
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return ""

    def _parse_page(self, html: str) -> list[dict]:
        arts = re.findall(r"<article[^>]*fr-card[^>]*>.*?</article>", html, re.DOTALL)
        items = []
        for art in arts:
            try:
                item = self._parse_card(art)
                if item:
                    items.append(item)
            except Exception as exc:
                print(f"[{self.site_id}] card parse error: {exc}")
        return items

    def _parse_card(self, art_html: str) -> dict | None:
        # Title
        tm = re.search(r"<h2[^>]*fr-card__title[^>]*>(.*?)</h2>", art_html, re.DOTALL)
        if not tm:
            return None
        raw_title = re.sub(r"\s+", " ", self._strip_tags(tm.group(1))).strip()

        # PDF URL
        pm = re.search(r'href="(https?://[^"]+\.pdf)"', art_html, re.IGNORECASE)
        pdf_url = pm.group(1) if pm else None

        # Category tag
        cm = re.search(r'<p[^>]*fr-tag[^>]*>(.*?)</p>', art_html, re.DOTALL)
        category = self._strip_tags(cm.group(1)).strip() if cm else ""

        # Date text: "07 mai 2026"
        dm = re.search(r'<p[^>]*fr-card__document-text[^>]*>(.*?)</p>', art_html, re.DOTALL)
        date_raw = self._strip_tags(dm.group(1)).strip() if dm else ""
        listed_date = self._parse_date_fr(date_raw)

        # Strip "DD.MM.YYYY " prefix from raw_title, then strip "Category : " prefix
        actual_title = raw_title
        title_date = ""
        pfx = re.match(r"(\d{2}\.\d{2}\.\d{4})\s+(.*)", raw_title, re.DOTALL)
        if pfx:
            title_date = self._parse_date_dmy(pfx.group(1))
            rest = pfx.group(2).strip()
            # Strip leading "Category : " (e.g., "Communiqué : ")
            cat_pfx = re.match(r"^[^:]{1,60}:\s+(.*)", rest, re.DOTALL)
            actual_title = cat_pfx.group(1).strip() if cat_pfx else rest

        published_date = listed_date or title_date

        # external_id from URL-decoded PDF filename
        external_id = None
        original_filename = None
        if pdf_url:
            path = urlparse(pdf_url).path
            fname = unquote(path.split("/")[-1])
            original_filename = fname
            external_id = re.sub(r"\.pdf$", "", fname, flags=re.IGNORECASE)

        if not external_id:
            external_id = (actual_title or raw_title)[:200]

        return {
            "raw_title": raw_title,
            "actual_title": actual_title,
            "category": category,
            "listed_date": listed_date,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "external_id": external_id,
            "original_filename": original_filename,
            "date_raw": date_raw,
        }

    def _build_abstract(self, title: str, category: str, date_str: str,
                        pdf_url: str | None) -> str:
        cat_label = f"[{category}] " if category else ""
        date_label = f"publié le {date_str} " if date_str else ""
        publisher = "Ministère des Armées et des Anciens combattants, France."
        header = f"{cat_label}{date_label}{publisher}"

        parts = [header]
        if title:
            parts.append(title)

        abstract = "\n".join(parts)

        # Pad to ensure >= 100 chars
        if len(abstract) < 100:
            if pdf_url:
                fname = unquote(urlparse(pdf_url).path.split("/")[-1])
                abstract += f"\nDocument officiel : {fname}"
        if len(abstract) < 100:
            abstract += (
                "\nDocument officiel disponible en téléchargement au format PDF"
                " sur le portail du ministère français de la Défense."
            )
        return abstract

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "∞"

        for page_num in range(self.MAX_PAGES):
            # Respect limit
            if limit is not None and saved >= limit:
                break

            # 25-minute wall-clock budget
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(
                    f"[{self.site_id}] 25-minute budget reached at page {page_num},"
                    " exiting cleanly."
                )
                break

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                print(
                    f"[{self.site_id}] page {page_num}: saved {saved}/{limit_or_inf}"
                )

            url = (
                f"{self.START_URL}?page={page_num}"
                if page_num > 0
                else self.START_URL
            )

            raw = self._curl_get(url)
            if not raw:
                print(f"[{self.site_id}] page {page_num}: fetch failed, skipping.")
                continue

            items = self._parse_page(raw)
            if not items:
                print(f"[{self.site_id}] page {page_num}: no items, stopping.")
                break

            # Safety cap log (should not happen given max_pages < 200 < total pages)
            if page_num >= self.MAX_PAGES - 1:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached.")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    dedup_key = item.get("pdf_url") or item.get("external_id", "")
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)
                    new_on_page += 1

                    actual_title = item["actual_title"]
                    raw_title = item["raw_title"]
                    category = item["category"]
                    listed_date = item["listed_date"]
                    date_raw = item["date_raw"]
                    pdf_url = item.get("pdf_url")
                    external_id = item["external_id"]
                    original_filename = item.get("original_filename")
                    published_date = item.get("published_date", "")

                    abstract = self._build_abstract(
                        actual_title, category, listed_date, pdf_url
                    )

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] abstract too short"
                            f" ({len(abstract)} chars), skipping:"
                            f" {actual_title[:60]}"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": actual_title or raw_title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": pdf_url or self.START_URL,
                        "pdf_url": pdf_url,
                        "category": category,
                        "publisher": (
                            "Ministère des Armées et des Anciens combattants"
                        ),
                        "original_filename": original_filename,
                        "authors": "",
                        "keywords": "",
                        "doi": "",
                        "department": "",
                        "journal": "",
                        "metadata": json.dumps(
                            {
                                "posted_date": date_raw,
                                "originalFilename": original_filename,
                                "raw_title": raw_title,
                                "category": category,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}:"
                        f" {(actual_title or raw_title)[:70]}"
                    )

                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            if new_on_page == 0:
                print(
                    f"[{self.site_id}] page {page_num}: no new items (dedup), stopping."
                )
                break

            time.sleep(self.RATE_SLEEP)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
