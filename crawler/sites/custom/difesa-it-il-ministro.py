# -*- coding: utf-8 -*-
"""Crawler for Italian Ministry of Defence press notes (Note Stampa).

Source: https://www.difesa.it/il-ministro/note-stampa/elenco/index.html
Data:   static JSON at /data/il-ministro/note-stampa/elenco.json (95 items)
Detail: /il-ministro/note-stampa/<slug>/<id>.html
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.difesa.it"
_LIST_JSON_URL = "https://www.difesa.it/data/il-ministro/note-stampa/elenco.json"
_ABSTRACT_MIN_CHARS = 50
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute budget
_SAFETY_CAP_PAGES = 200       # not really used (single JSON), but kept for spec

_IT_MONTHS = {
    "gen": "01", "feb": "02", "mar": "03", "apr": "04",
    "mag": "05", "giu": "06", "lug": "07", "ago": "08",
    "set": "09", "ott": "10", "nov": "11", "dic": "12",
}


def _parse_it_date(sdata: str) -> str:
    """Convert Italian date '05 mag 2026' → '2026-05-05'. Returns '' on failure."""
    if not sdata:
        return ""
    parts = sdata.strip().split()
    if len(parts) == 3:
        day, mon, year = parts
        m = _IT_MONTHS.get(mon.lower())
        if m:
            return f"{year}-{m}-{day.zfill(2)}"
    return ""


def _parse_iso_date(raw: str) -> str:
    """Extract YYYY-MM-DD from ISO datetime string like '2026-05-05T00:00:00'."""
    if raw and len(raw) >= 10:
        return raw[:10]
    return ""


def _make_soup(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(html_fragment: str) -> str:
    """Strip tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


class DifesaItIlMinistroCrawler(BaseCrawler):
    site_id = "difesa-it-il-ministro"
    site_name = "Custom: difesa-it-il-ministro"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45, referer: str | None = None) -> str | None:
        """Fetch URL via curl with up to 3 retries. Returns raw text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/json,*/*;q=0.8",
            "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                raw = result.stdout.decode("utf-8", errors="replace").strip()
                if raw:
                    return raw
                wait = [1, 3, 9][attempt]
                print(f"[difesa-it-il-ministro] empty response (attempt {attempt+1}/3) for {url}, retry in {wait}s")
                time.sleep(wait)
            except subprocess.TimeoutExpired:
                print(f"[difesa-it-il-ministro] timeout (attempt {attempt+1}/3) for {url}")
                if attempt < 2:
                    time.sleep([1, 3, 9][attempt])
            except Exception as exc:
                print(f"[difesa-it-il-ministro] curl error (attempt {attempt+1}/3) for {url}: {exc}")
                if attempt < 2:
                    time.sleep([1, 3, 9][attempt])
        return None

    # ------------------------------------------------------------------
    # Detail-page scraping
    # ------------------------------------------------------------------

    def _extract_abstract(self, detail_url: str) -> str:
        """Fetch the detail page and return the main body text."""
        raw = self._curl(detail_url, referer=_BASE_URL + "/il-ministro/note-stampa/elenco/index.html")
        if not raw:
            return ""

        soup = _make_soup(raw)
        if soup is None:
            # Last-ditch regex extraction
            match = re.search(
                r'col-xl-8 col-lg-8[^>]*>(.*?)</div>',
                raw, re.DOTALL | re.IGNORECASE
            )
            return _strip_html(match.group(1)) if match else ""

        # The content lives inside div.col-xl-8.col-lg-8
        container = soup.find("div", class_=lambda c: c and "col-xl-8" in c and "col-lg-8" in c)
        if container is None:
            # Fallback: grab #indexdiv
            container = soup.find("div", id="indexdiv")
        if container is None:
            container = soup.find("div", id="main")

        if container is None:
            return ""

        # Collect all paragraph/block text; skip nav/breadcrumb noise
        parts = []
        for tag in container.find_all(["p", "div", "li", "h2", "h3"]):
            # Skip breadcrumb items and date boxes
            classes = " ".join(tag.get("class", []))
            if any(k in classes for k in ("breadcrumb", "colorDate", "row")):
                continue
            text = tag.get_text(separator=" ", strip=True)
            if text and len(text) > 20:
                parts.append(text)

        abstract = " ".join(parts)
        abstract = re.sub(r"\s+", " ", abstract).strip()
        return abstract

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Fetch the static JSON list, then scrape each detail page.

        Parameters
        ----------
        limit:
            Maximum number of records to save. None = all.
        """
        start_time = time.monotonic()
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set[str] = set()

        # ---- Fetch the full JSON list (single request, no server-side pagination) ----
        print(f"[difesa-it-il-ministro] Fetching list JSON from {_LIST_JSON_URL}")
        raw_json = self._curl(_LIST_JSON_URL)
        if not raw_json:
            print("[difesa-it-il-ministro] Failed to fetch list JSON. Aborting.")
            return 0

        try:
            items = json.loads(raw_json)
        except json.JSONDecodeError as exc:
            print(f"[difesa-it-il-ministro] JSON decode error: {exc}")
            return 0

        if not items:
            print("[difesa-it-il-ministro] Empty item list. Aborting.")
            return 0

        print(f"[difesa-it-il-ministro] Found {len(items)} items in list JSON.")

        # Sort by contentitemid descending (newest first = highest ID)
        try:
            items = sorted(items, key=lambda x: int(x.get("contentitemid", 0)), reverse=True)
        except Exception:
            pass  # keep original order if sort fails

        # ---- Process each item ----
        for idx, item in enumerate(items):
            if saved >= limit_or_inf:
                break

            # Wall-clock budget check
            elapsed = time.monotonic() - start_time
            if elapsed > _MAX_WALL_SECONDS:
                print(f"[difesa-it-il-ministro] Wall-clock budget reached ({elapsed:.0f}s). Stopping.")
                break

            try:
                item_id = str(item.get("contentitemid", ""))
                raw_url = item.get("url", "")
                detail_url = _BASE_URL + raw_url if raw_url.startswith("/") else raw_url
                title = (item.get("titolo") or "").strip()

                if not item_id or not raw_url:
                    print(f"[difesa-it-il-ministro] Item {idx}: missing id or url, skipping.")
                    continue

                if detail_url in seen_urls:
                    print(f"[difesa-it-il-ministro] Item {idx}: duplicate URL {detail_url}, skipping.")
                    continue
                seen_urls.add(detail_url)

                if not title:
                    print(f"[difesa-it-il-ministro] Item {idx} (id={item_id}): no title, skipping.")
                    continue

                # Dates
                published_date = _parse_iso_date(item.get("contentitemdata", ""))
                listed_date = _parse_it_date(item.get("sdata", ""))

                # Fetch detail page for abstract
                time.sleep(self._delay)
                abstract = self._extract_abstract(detail_url)

                if len(abstract) < _ABSTRACT_MIN_CHARS:
                    print(f"[difesa-it-il-ministro] Item {item_id}: abstract too short "
                          f"({len(abstract)} chars), skipping.")
                    continue

                # Build metadata dict
                meta = {
                    "posted_date": item.get("sdata"),
                    "contentitemid": item.get("contentitemid"),
                    "ordine": item.get("ordine"),
                    "luogo": item.get("luogo"),
                    "anno": item.get("anno"),
                    "comunicatonumero": item.get("comunicatonumero"),
                    "areatematica": item.get("areatematica"),
                    "inaggiornamento": item.get("inaggiornamento"),
                    "sottotitolo": item.get("sottotitolo"),
                    "video": item.get("video"),
                    "metatags": item.get("metatags"),
                }

                paper = {
                    "site_id": self.site_id,
                    "external_id": item_id,
                    "post_number": item_id,
                    "title": title,
                    "abstract": abstract,
                    "url": detail_url,
                    "pdf_url": None,
                    "published_date": published_date,
                    "posted_date": listed_date,
                    "authors": None,
                    "publisher": "Ministero della Difesa",
                    "department": None,
                    "journal": None,
                    "keywords": None,
                    "category": item.get("areatematica") or "",
                    "doi": None,
                    "original_filename": None,
                    "metadata": json.dumps(meta, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                counter = f"{saved}/{limit}" if limit is not None else str(saved)
                print(f"[difesa-it-il-ministro] Saved {counter}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[difesa-it-il-ministro] Item {idx} failed: {exc}")
                continue

        print(f"[difesa-it-il-ministro] Done. Total saved: {saved}")
        return saved
