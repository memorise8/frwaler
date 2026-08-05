# -*- coding: utf-8 -*-
"""Crawler for unidata.gv.at Publikationen.

unidata.gv.at was migrated off SharePoint (the old
``/Pages/auswertungen.aspx`` + ``/_api/web/lists(guid'...')/items`` REST
API this crawler originally used now 500s / 404s — the whole "Auswertungen"
SharePoint document library is gone). The site is now a plain server-rendered
CMS; its "Publikationen" section (https://unidata.gv.at/publikationen,
paginated as ``/publikationen/p{N}``, "1 von 20" pages seen ≈500 items) lists
``a.publication-card[href]`` cards that link directly to a PDF and carry the
title (``title`` attribute / ``h3.headline``), a ``span.date`` (DD.MM.YYYY)
and file type/size — no separate detail page is needed, everything required
is already on the list page.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape
from urllib.parse import unquote, urlparse

try:
    from bs4 import BeautifulSoup
except Exception:  # pragma: no cover - regex fallbacks still work
    BeautifulSoup = None

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


_LIST_URL = "https://unidata.gv.at/publikationen"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_CHARS = 50
_PUBLISHER = "unidata.gv.at"
_INTRO = (
    "Unidata-Publikation im Bereich Hochschulstatistik/Hochschulbereich "
    "Österreichs."
)


class UnidataGvAtPagesCrawler(BaseCrawler):
    site_id = "unidata-gv-at-pages"
    site_name = "Custom: unidata-gv-at-pages"
    base_url = "https://unidata.gv.at"

    detail_delay = 1.0

    def crawl(self, limit=None):
        if limit is not None and limit <= 0:
            return 0

        started_at = time.time()
        saved = 0
        page = 1
        seen_urls = set()
        limit_label = limit if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if time.time() - started_at > _WALL_BUDGET_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break
            if page % 10 == 0 or page == 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            page_url = _LIST_URL if page == 1 else f"{_LIST_URL}/p{page}"
            raw = self._curl_text(page_url)
            if not raw:
                print(f"[{self.site_id}] page {page}: list API returned no data; stopping")
                break

            cards = self._parse_cards(raw)
            if not cards:
                print(f"[{self.site_id}] page {page}: no records; done")
                break

            new_urls_on_page = 0
            for card in cards:
                if limit is not None and saved >= limit:
                    break

                pdf_url = card.get("pdf_url")
                if not pdf_url:
                    continue
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_urls_on_page += 1

                try:
                    time.sleep(float(getattr(self, "detail_delay", self._delay)))

                    paper = self._build_paper(card)
                    if not paper:
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{self.site_id}] item {card.get('external_id')} short abstract "
                              f"({len(abstract)} chars); skipping")
                        continue

                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {card.get('external_id')} failed: {exc}")
                    continue

            if new_urls_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new URLs; stopping to avoid pagination loop")
                break
            if not self._has_next_page(raw, page):
                print(f"[{self.site_id}] page {page}: last page reached")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_text(self, url, *, retries=3, max_time=45):
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--compressed",
            "--max-time",
            str(max_time),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H",
            "Accept-Language: de-AT,de;q=0.9,en;q=0.8",
            url,
        ]
        waits = (1, 3, 9)
        last_error = None
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=max_time + 10,
                    check=False,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")
                stderr = result.stderr.decode("utf-8", errors="replace")
                if result.returncode == 0 and stdout.strip():
                    return stdout
                last_error = stderr.strip() or f"curl returned {result.returncode}"
            except Exception as exc:
                last_error = str(exc)

            if attempt < retries - 1:
                print(
                    f"[{self.site_id}] network error for {url}: {last_error}; "
                    f"retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] network failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing and mapping
    # ------------------------------------------------------------------

    def _parse_cards(self, raw):
        soup = self._make_soup(raw)
        if soup is None:
            return []

        cards = []
        for a in soup.select("a.publication-card[href]"):
            href = (a.get("href") or "").strip()
            if not href or not href.lower().endswith(".pdf"):
                continue

            title = self._string(a.get("title"))
            if not title:
                headline = a.select_one("h3.headline")
                title = self._string(headline.get_text(" ", strip=True)) if headline else None
            if not title:
                continue

            date_node = a.select_one("span.date")
            date_raw = self._string(date_node.get_text(" ", strip=True)) if date_node else None
            listed_date = self._iso_date(date_raw)

            size_node = a.select_one("span.size")
            file_size = self._string(size_node.get_text(" ", strip=True)) if size_node else None

            page_size_node = a.select_one("span.page-size")
            page_size = self._string(page_size_node.get_text(" ", strip=True)) if page_size_node else None

            filename = self._filename_from_path(href)
            external_id = self._external_id_from_filename(filename or href)

            cards.append({
                "external_id": external_id,
                "title": self._clean_title(title),
                "pdf_url": href,
                "original_filename": filename,
                "listed_date": listed_date,
                "listed_date_raw": date_raw,
                "file_size": file_size,
                "page_size": page_size,
            })
        return cards

    def _build_paper(self, card):
        external_id = card["external_id"]
        title = card["title"]
        pdf_url = card["pdf_url"]

        abstract = self._make_abstract(
            title=title,
            filename=card.get("original_filename"),
            listed_date=card.get("listed_date_raw"),
            page_size=card.get("page_size"),
            file_size=card.get("file_size"),
        )

        keywords = self._join_keywords(["unidata", "Hochschulstatistik", "Publikation"])

        metadata = {
            "posted_date": card.get("listed_date_raw"),
            "posted_date_iso": card.get("listed_date"),
            "originalFilename": card.get("original_filename"),
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "page_size": card.get("page_size"),
            "file_size": card.get("file_size"),
            "list_page": _LIST_URL,
        }

        return {
            "id": f"{self.site_id}-{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": card.get("listed_date"),
            "listed_date": card.get("listed_date"),
            "posted_date": card.get("listed_date"),
            "authors": None,
            "publisher": _PUBLISHER,
            "department": None,
            "journal": None,
            "url": pdf_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": "Publikation",
            "doi": None,
            "original_filename": card.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _make_abstract(self, *, title, filename, listed_date, page_size, file_size):
        parts = [
            "Unidata-Publikation aus dem österreichischen Hochschulbereich.",
            f"Titel: {title}.",
        ]
        if filename:
            parts.append(f"Berichtsdatei: {filename}.")
        if listed_date:
            parts.append(f"Veröffentlicht: {listed_date}.")
        if page_size:
            parts.append(f"Umfang: {page_size}.")
        if file_size:
            parts.append(f"Dateigröße: {file_size}.")
        parts.append(_INTRO)
        return self._clean_text(" ".join(parts))

    def _has_next_page(self, raw, current_page):
        soup = self._make_soup(raw)
        if soup is None:
            return False
        next_link = soup.select_one("a.pagination-link .icon.next")
        if next_link is not None:
            return True
        for a in soup.select("a.pagination-link[href]"):
            href = a.get("href") or ""
            if href.rstrip("/").endswith(f"/p{current_page + 1}"):
                return True
        return False

    def _make_soup(self, raw):
        if BeautifulSoup is None:
            return None
        last_error = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                last_error = exc
                continue
        print(f"[{self.site_id}] BeautifulSoup parser chain failed: {last_error}")
        return None

    @staticmethod
    def _clean_title(value):
        text = UnidataGvAtPagesCrawler._clean_text(value)
        return re.sub(r"\.(?:xl3wbz|xlsx?|csv|pdf)\s*$", "", text, flags=re.I)

    @staticmethod
    def _clean_text(value):
        if value is None:
            return ""
        text = unescape(str(value))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    @staticmethod
    def _string(value):
        text = UnidataGvAtPagesCrawler._clean_text(value)
        return text or None

    @staticmethod
    def _filename_from_path(path):
        if not path:
            return None
        tail = urlparse(str(path)).path.rstrip("/").split("/")[-1]
        return unquote(tail) if tail else None

    @staticmethod
    def _external_id_from_filename(filename):
        if not filename:
            return None
        stem = re.sub(r"\.(?:pdf|xlsx?|csv)$", "", filename, flags=re.I)
        return stem or filename

    @staticmethod
    def _iso_date(value):
        text = UnidataGvAtPagesCrawler._clean_text(value)
        if not text:
            return None

        m = re.search(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})T", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(20\d{2}|19\d{2})\b", text)
        if m:
            day, month, year = m.groups()
            return f"{year}-{int(month):02d}-{int(day):02d}"

        m = re.search(r"\b(20\d{2}|19\d{2})-(\d{2})-(\d{2})\b", text)
        if m:
            return m.group(0)

        return None

    @staticmethod
    def _join_keywords(values):
        seen = set()
        out = []
        for value in values:
            text = UnidataGvAtPagesCrawler._clean_text(value)
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(text)
        return ", ".join(out) if out else None
