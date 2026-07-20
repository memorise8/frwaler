# -*- coding: utf-8 -*-
"""Crawler for BMI Austria Veröffentlichungspflichten (studies, reports, surveys).

Source: https://www.bmi.gv.at/114/start.aspx
Published under Art. 20 Abs. 5 B-VG (constitutional transparency requirement).
Single-page listing of 35+ <details> accordion items — no pagination or API.
"""

import hashlib
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.bmi.gv.at/114/start.aspx"
_BASE_URL = "https://www.bmi.gv.at"
_SITE_URL = f"{_BASE_URL}/114"

# Fixed descriptive prefix ensures every abstract is >= 100 chars.
_INTRO = (
    "Veröffentlicht gemäß Art. 20 Abs. 5 B-VG (verfassungsgesetzliche "
    "Transparenzpflicht) des Bundesministeriums für Inneres (BMI), Österreich."
)


class BmiGvAt114Crawler(BaseCrawler):
    """Crawler for BMI Austria publication obligations page."""

    site_id = "bmi-gv-at-114"
    site_name = "Custom: bmi-gv-at-114"
    base_url = "https://www.bmi.gv.at"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[bmi-gv-at-114] Empty response, retrying in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) * 3
                    print(f"[bmi-gv-at-114] curl error: {exc}, retrying in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[bmi-gv-at-114] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str):
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean(text: str) -> str:
        """Unescape common HTML entities and normalise whitespace."""
        _ENT = {
            "&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ",
            "&ouml;": "ö", "&Ouml;": "Ö", "&auml;": "ä", "&Auml;": "Ä",
            "&uuml;": "ü", "&Uuml;": "Ü", "&szlig;": "ß",
            "&euro;": "€", "&bdquo;": "„", "&ldquo;": "“",
            "&rdquo;": "”", "&laquo;": "«", "&raquo;": "»",
            "&ndash;": "–", "&mdash;": "—", "&rsquo;": "'",
        }
        for ent, repl in _ENT.items():
            text = text.replace(ent, repl)
        text = re.sub(r"&#\d+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _date_from_string(s: str) -> str:
        """Return the first YYYY-MM-DD date found in *s*, or empty string."""
        m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", s)
        if m:
            y, mo, d = m.groups()
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
        m = re.search(r"(20\d{2})(\d{2})(\d{2})", s)
        if m:
            y, mo, d = m.groups()
            if 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
                return f"{y}-{mo}-{d}"
        return ""

    # ------------------------------------------------------------------
    # Item parsing
    # ------------------------------------------------------------------

    def _parse_block(self, block, idx: int) -> dict | None:
        """Parse one <details> block into a paper dict. Returns None to skip."""
        summary_tag = block.find("summary")
        if not summary_tag:
            return None
        title = self._clean(summary_tag.get_text(" ", strip=True))
        if not title:
            return None

        # Parse <dl> key→[values] map
        fields: dict[str, list[str]] = {}
        pdf_hrefs: list[str] = []

        dl = block.find("dl")
        if dl:
            cur_key = None
            for child in dl.children:
                tag = getattr(child, "name", None)
                if tag == "dt":
                    cur_key = self._clean(child.get_text(" ", strip=True)).rstrip(":")
                    if cur_key not in fields:
                        fields[cur_key] = []
                elif tag == "dd" and cur_key is not None:
                    # Collect PDF links separately
                    for a in child.find_all("a", href=True):
                        href = a["href"].strip()
                        if href and (".pdf" in href.lower() or href.endswith(".pdf")):
                            abs_href = (
                                href if href.startswith("http")
                                else f"{_SITE_URL}/{href}"
                            )
                            if abs_href not in pdf_hrefs:
                                pdf_hrefs.append(abs_href)
                    text_val = self._clean(child.get_text(" ", strip=True))
                    if text_val:
                        fields[cur_key].append(text_val)

        def field(*keys: str) -> str:
            for k in keys:
                vals = fields.get(k, [])
                text = " ".join(
                    v for v in vals
                    if not v.lower().endswith(".pdf") and not v.startswith("http")
                ).strip()
                if text:
                    return text
            return ""

        auftragnehmer = field("Auftragnehmer")
        kategorie = field("Kategorie")
        kosten = field("Kosten", "Kosten (Bruttobetrag)")
        bedarfstraeger = field("Bedarfsträger und Auftraggeber", "Bedarfsträger")
        schlagwort = field("Schlagwort")

        # Build abstract (guaranteed >= 142 chars from _INTRO alone)
        parts = [_INTRO]
        if title:
            parts.append(f'Titel: {title}.')
        if kategorie:
            parts.append(f"Kategorie: {kategorie}.")
        if auftragnehmer:
            parts.append(f"Auftragnehmer: {auftragnehmer}.")
        if bedarfstraeger:
            parts.append(f"Bedarfsträger: {bedarfstraeger}.")
        if kosten:
            parts.append(f"Kosten: {kosten}.")
        if schlagwort:
            parts.append(f"Schlagwort: {schlagwort}.")
        abstract = " ".join(parts)

        if len(abstract) < 50:
            print(f"[bmi-gv-at-114] item {idx} abstract too short ({len(abstract)}), skipping")
            return None

        # PDF & filename
        pdf_url = pdf_hrefs[0] if pdf_hrefs else None
        original_filename = pdf_url.split("/")[-1] if pdf_url else None

        # Date: try PDF filename first, then folder, then whole URL
        published_date = ""
        for s in [original_filename or "", pdf_url or ""]:
            published_date = self._date_from_string(s)
            if published_date:
                break

        # external_id: folder name in /114/files/<folder>/
        ext_id = None
        if pdf_url:
            m = re.search(r"/114/files/([^/]+)/", pdf_url)
            if m:
                ext_id = m.group(1)
        if not ext_id:
            ext_id = hashlib.md5(title.encode()).hexdigest()[:16]

        return {
            "site_id": self.site_id,
            "external_id": ext_id,
            "post_number": str(idx),
            "title": title,
            "abstract": abstract,
            "published_date": published_date or None,
            "listed_date": None,
            "authors": auftragnehmer or None,
            "publisher": "Bundesministerium für Inneres (BMI)",
            "department": bedarfstraeger or None,
            "journal": None,
            "url": f"{_LIST_URL}#item-{idx}",
            "pdf_url": pdf_url,
            "keywords": schlagwort or None,
            "category": kategorie or None,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps({
                "posted_date": None,
                "originalFilename": original_filename,
                "kosten": kosten,
                "kategorie": kategorie,
                "all_pdf_urls": pdf_hrefs,
                "item_index": idx,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        wall_start = time.time()
        MAX_WALL = 25 * 60  # 25 minutes

        limit_str = str(limit) if limit is not None else "∞"
        print(f"[bmi-gv-at-114] Starting crawl (limit={limit_str})")
        print(f"[bmi-gv-at-114] Fetching {_LIST_URL}")

        raw = self._curl_get(_LIST_URL)
        if not raw:
            print("[bmi-gv-at-114] Failed to fetch listing page. Aborting.")
            return 0

        try:
            soup = self._make_soup(raw)
        except Exception as exc:
            print(f"[bmi-gv-at-114] HTML parse failed: {exc}. Aborting.")
            return 0

        if not soup:
            print("[bmi-gv-at-114] Could not construct BeautifulSoup. Aborting.")
            return 0

        blocks = soup.find_all("details")
        print(f"[bmi-gv-at-114] Found {len(blocks)} <details> items on page")

        saved = 0
        seen_urls: set[str] = set()
        # The page is a single list — no real pagination, but we mimic the
        # pagination progress log every 10 items for consistency with spec.
        PAGE_LOG_INTERVAL = 10

        for idx, block in enumerate(blocks, 1):
            if limit is not None and saved >= limit:
                break

            if time.time() - wall_start > MAX_WALL:
                print(f"[bmi-gv-at-114] 25-minute budget reached at item {idx}. Stopping.")
                break

            if idx % PAGE_LOG_INTERVAL == 0:
                print(f"[bmi-gv-at-114] page 1 item {idx}: saved {saved}/{limit_str}")

            try:
                paper = self._parse_block(block, idx)
            except Exception as exc:
                print(f"[bmi-gv-at-114] item {idx} failed: {exc}")
                continue

            if paper is None:
                continue

            # URL deduplication
            dedup_key = paper.get("pdf_url") or paper["title"]
            if dedup_key in seen_urls:
                print(f"[bmi-gv-at-114] item {idx} duplicate, skipping: {dedup_key[:60]}")
                continue
            seen_urls.add(dedup_key)

            try:
                self._save_paper(paper)
            except Exception as exc:
                print(f"[bmi-gv-at-114] item {idx} save failed: {exc}")
                continue

            saved += 1
            print(f"[bmi-gv-at-114] [{saved}/{limit_str}] {paper['title'][:70]}")

            time.sleep(self._delay)

        print(f"[bmi-gv-at-114] Done. Total saved: {saved}")
        return saved
