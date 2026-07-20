# -*- coding: utf-8 -*-
"""Crawler for Socialstyrelsen (Sweden) English-language publications.

Strategy:
  - Single GET to /en/publications/ loads all 3800+ items as React props JSON.
  - Filter by languageId == '137' (English) → ~264 publications.
  - Per-item: fetch detail page (HTML) to get PDF URL + publication date.
  - Extract abstract from PDF first-5-pages text via pdfplumber.
  - Skip items whose abstract < 50 chars (no usable content).
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import time
from typing import Optional

# ── third-party (all available in .venv) ──────────────────────────────────────
try:
    import pdfplumber as _pdfplumber
    _HAS_PDFPLUMBER = True
except ImportError:
    _HAS_PDFPLUMBER = False

try:
    from bs4 import BeautifulSoup as _BS
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from crawler.base_crawler import BaseCrawler  # noqa: E402

# ── static ID → name mappings (from /publikationer/ page, 2026-05-14) ─────────
_SUBJECT_MAP: dict[int, str] = {
    154: "Barn och familj",
    155: "Donation",
    156: "Dödsfall",
    157: "E-hälsa",
    158: "Ekonomiskt bistånd",
    159: "Fallolyckor",
    160: "Funktionshinder",
    161: "Hemlöshet",
    162: "Hjälpmedel",
    163: "Jämlik vård och omsorg",
    164: "Kvinnors hälsa",
    165: "Läkemedel",
    166: "Missbruk och beroende",
    167: "Palliativ vård",
    168: "Psykisk ohälsa",
    169: "Stöd till anhöriga",
    170: "Våld- och brott",
    171: "Vårdhygien",
    172: "Äldre",
    559: "Covid-19",
    563: "Beredskap",
    564: "Sällsynta hälsotillstånd",
    565: "Tandvård",
}

_MANDATORY_MAP: dict[int, str] = {
    113: "Hälso- och sjukvård",
    114: "Socialtjänst",
    115: "Tandvård",
}

_LANGUAGE_MAP: dict[int, str] = {
    132: "Arabic", 133: "Albanian", 137: "English", 138: "Finnish",
    140: "Kurdish", 142: "Polish", 143: "Persian", 144: "Russian",
    145: "Somali", 146: "Spanish", 147: "Swedish", 148: "Turkish",
    149: "German", 517: "Meänkieli", 518: "Amarinja", 519: "Amharic",
    520: "Bosnian", 521: "Dari", 522: "French", 523: "Yiddish",
    525: "Kurmanji", 526: "Lule Sami", 528: "Pashto", 530: "Romani (Arli)",
    531: "Romani (Kalderas)", 532: "Romani (Kale)", 533: "Romani (Lovari)",
    534: "Sami", 535: "Serbian", 536: "Sorani", 537: "South Sami",
    538: "Tamil", 539: "Tigrinya", 540: "Ukrainian", 541: "Urdu",
    543: "Croatian",
}


class SocialstyrelseSEENcrawler(BaseCrawler):
    """Crawler for Socialstyrelsen (Sweden) English publications."""

    site_id = "socialstyrelsen-se-en"
    site_name = "Custom: socialstyrelsen-se-en"
    base_url = "https://www.socialstyrelsen.se"

    _LIST_URL = "https://www.socialstyrelsen.se/en/publications/"
    _ENGLISH_LANG_ID = "137"
    _MAX_WALL_SECS = 25 * 60  # 25-minute hard budget
    _PROGRESS_EVERY = 10       # log every N pages (items, really)

    # ── internal HTTP helpers ─────────────────────────────────────────────────

    def _curl_text(self, url: str, retries: int = 3) -> Optional[str]:
        """GET url → decoded text string, or None on failure."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    ["curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                     "-H", f"User-Agent: {self.USER_AGENT}", url],
                    capture_output=True, timeout=35,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                wait = (2 ** attempt)  # 1, 2, 4
                if attempt < retries - 1:
                    print(f"[{self.site_id}] GET empty/error ({url[:60]}), retry {attempt+1}/{retries} in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                wait = (2 ** attempt)
                if attempt < retries - 1:
                    print(f"[{self.site_id}] curl error: {exc}, retry {attempt+1}/{retries} in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts for {url[:80]}: {exc}")
        return None

    def _curl_bytes(self, url: str, retries: int = 3) -> Optional[bytes]:
        """GET url → raw bytes, or None on failure."""
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    ["curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
                     "-H", f"User-Agent: {self.USER_AGENT}", url],
                    capture_output=True, timeout=65,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout
                wait = (2 ** attempt)
                if attempt < retries - 1:
                    print(f"[{self.site_id}] bytes GET empty/error ({url[:60]}), retry {attempt+1}/{retries} in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                wait = (2 ** attempt)
                if attempt < retries - 1:
                    print(f"[{self.site_id}] curl bytes error: {exc}, retry {attempt+1}/{retries} in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl bytes failed: {exc}")
        return None

    # ── BeautifulSoup with fallback chain ─────────────────────────────────────

    @staticmethod
    def _make_soup(html: str):
        if not _HAS_BS4:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return _BS(html, parser)
            except Exception:
                continue
        return None

    # ── list-page parsing ─────────────────────────────────────────────────────

    def _fetch_all_items(self) -> list:
        """Return the full children list from the React props of /en/publications/."""
        html = self._curl_text(self._LIST_URL)
        if not html:
            print(f"[{self.site_id}] Failed to fetch list page")
            return []

        # The page embeds: ReactDOM.hydrate(React.createElement(SOS.Components.PublicationListPage, {...}
        m = re.search(
            r"React(?:DOM\.hydrate|DOM\.render)\("
            r"React\.createElement\(SOS\.Components\.PublicationListPage,\s*(\{)",
            html,
        )
        if not m:
            print(f"[{self.site_id}] PublicationListPage component not found in page")
            return []

        raw = html[m.start(1):]
        depth, end = 0, 0
        for j, ch in enumerate(raw):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j + 1
                    break

        try:
            props = json.loads(raw[:end])
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] Failed to parse page JSON: {exc}")
            return []

        children = props.get("children", [])
        print(f"[{self.site_id}] List page: {len(children)} total items")
        return children

    # ── detail-page parsing ───────────────────────────────────────────────────

    def _parse_detail(self, html: str) -> dict:
        """Return {pdf_url, published_date} extracted from a publication detail page."""
        result: dict = {"pdf_url": None, "published_date": None}

        soup = self._make_soup(html)
        if soup:
            # PDF link: <a class="publication-wrapper__main-doc …" href="/contentassets/…pdf">
            for tag in soup.find_all("a", href=True):
                href = tag.get("href", "")
                cls = " ".join(tag.get("class") or [])
                if "publication-wrapper__main-doc" in cls and href.lower().endswith(".pdf"):
                    result["pdf_url"] = href
                    break
            # Fallback: any /contentassets/… .pdf link
            if not result["pdf_url"]:
                for tag in soup.find_all("a", href=re.compile(r"/contentassets/.*\.pdf", re.I)):
                    result["pdf_url"] = tag["href"]
                    break

            # Date: <span class="date">Publicerad: 2026-04-21</span>
            span = soup.find("span", class_="date")
            if span:
                m = re.search(r"(\d{4}-\d{2}-\d{2})", span.get_text())
                if m:
                    result["published_date"] = m.group(1)
        else:
            # Pure-regex fallback
            m = re.search(
                r'href="(/contentassets/[^"]+\.pdf)"', html, re.I
            )
            if m:
                result["pdf_url"] = m.group(1)
            m = re.search(r'class="date"[^>]*>Publicerad:\s*(\d{4}-\d{2}-\d{2})', html)
            if m:
                result["published_date"] = m.group(1)

        return result

    # ── PDF text extraction ───────────────────────────────────────────────────

    def _extract_pdf_text(self, pdf_url: str) -> str:
        """Download PDF and extract text from the first 5 pages."""
        if not _HAS_PDFPLUMBER:
            return ""
        data = self._curl_bytes(pdf_url)
        if not data or len(data) < 512:
            return ""
        try:
            with _pdfplumber.open(io.BytesIO(data)) as pdf:
                parts = []
                for page in pdf.pages[:5]:
                    try:
                        t = page.extract_text() or ""
                        if t.strip():
                            parts.append(t)
                    except Exception:
                        continue
                return "\n\n".join(parts)
        except Exception as exc:
            print(f"[{self.site_id}] PDF extract error ({pdf_url[-60:]}): {exc}")
            return ""

    # ── main crawl ────────────────────────────────────────────────────────────

    def crawl(self, limit=None):
        """Crawl Socialstyrelsen English publications.

        Parameters
        ----------
        limit:
            Max number of records to save. None = unlimited.
        """
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # 1. Fetch full item list (all items are embedded in one page load)
        all_items = self._fetch_all_items()
        if not all_items:
            return 0

        # 2. Filter to English publications
        eng_items = [i for i in all_items if i.get("languageId") == self._ENGLISH_LANG_ID]
        print(f"[{self.site_id}] {len(eng_items)} English publications (languageId={self._ENGLISH_LANG_ID})")

        # Sort newest-first so incremental runs fill the most recent first
        eng_items.sort(key=lambda x: x.get("publishOnWebFrom", ""), reverse=True)

        seen_urls: set = set()
        saved = 0
        skipped = 0
        page_counter = 0  # each item == 1 "page" for progress logging

        for item in eng_items:
            # ── limit / budget checks ──────────────────────────────────────
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed > self._MAX_WALL_SECS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping at {saved} saved")
                break

            article_number = item.get("articleNumber", "")
            item_url = item.get("url", "")
            name = item.get("name", "")

            if not item_url:
                continue

            # ── deduplication ──────────────────────────────────────────────
            if item_url in seen_urls:
                continue
            seen_urls.add(item_url)
            page_counter += 1

            full_url = (
                f"{self.base_url}{item_url}"
                if item_url.startswith("/")
                else item_url
            )

            # ── per-item isolation ─────────────────────────────────────────
            try:
                time.sleep(self._delay)

                # Fetch detail page
                detail_html = self._curl_text(full_url)
                if not detail_html:
                    print(f"[{self.site_id}] Detail fetch failed for {article_number}, skipping")
                    continue

                detail = self._parse_detail(detail_html)
                pdf_rel = detail.get("pdf_url")
                pub_date = detail.get("published_date") or item.get("publishOnWebFrom", "")
                listed_date = item.get("publishOnWebFrom", "")

                # Build full PDF URL
                pdf_url: Optional[str] = None
                if pdf_rel:
                    pdf_url = (
                        f"{self.base_url}{pdf_rel}"
                        if pdf_rel.startswith("/")
                        else pdf_rel
                    )

                # Extract abstract from PDF
                abstract = ""
                if pdf_url:
                    abstract = self._extract_pdf_text(pdf_url)

                abstract = abstract.strip()

                # Skip items with unusably short abstracts
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] abstract too short ({len(abstract)} chars) "
                        f"for {article_number!r}, skipping"
                    )
                    skipped += 1
                    continue

                # ── build metadata fields ──────────────────────────────────
                subject_ids_raw = item.get("subjectsId", "") or ""
                subject_names = []
                for sid_str in subject_ids_raw.split(","):
                    sid_str = sid_str.strip()
                    if sid_str:
                        try:
                            subject_names.append(
                                _SUBJECT_MAP.get(int(sid_str), sid_str)
                            )
                        except ValueError:
                            subject_names.append(sid_str)
                keywords = ", ".join(subject_names)

                mandatory_id_raw = item.get("mandatoryId", "") or ""
                try:
                    category = _MANDATORY_MAP.get(int(mandatory_id_raw), "")
                except ValueError:
                    category = mandatory_id_raw

                lang_id_raw = item.get("languageId", "") or ""
                try:
                    lang_name = _LANGUAGE_MAP.get(int(lang_id_raw), lang_id_raw)
                except ValueError:
                    lang_name = lang_id_raw

                original_filename: Optional[str] = None
                if pdf_url:
                    original_filename = os.path.basename(pdf_url.split("?")[0])

                paper = {
                    "site_id": self.site_id,
                    "external_id": article_number,
                    "post_number": article_number,
                    "title": name,
                    "abstract": abstract,
                    "published_date": pub_date,
                    "listed_date": listed_date,
                    "url": full_url,                        # → meta_url via adapter
                    "pdf_url": pdf_url,
                    "publisher": "Socialstyrelsen",
                    "keywords": keywords,
                    "category": category,
                    "original_filename": original_filename,
                    "metadata": json.dumps({
                        "posted_date": listed_date,         # adapter extracts → posted_date
                        "articleNumber": article_number,
                        "languageId": lang_id_raw,
                        "languageName": lang_name,
                        "subjectsId": subject_ids_raw,
                        "mandatoryId": mandatory_id_raw,
                        "yearId": item.get("yearId", ""),
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] saved {saved}/{limit_str}: {name[:60]}")

                # Progress log every 10 items
                if page_counter % self._PROGRESS_EVERY == 0:
                    print(
                        f"[{self.site_id}] page {page_counter}: "
                        f"saved {saved}/{limit_str}  skipped={skipped}  "
                        f"elapsed={int(time.time()-start_time)}s"
                    )

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {article_number!r} failed: {exc}")
                continue

        print(
            f"[{self.site_id}] Done. saved={saved}, skipped={skipped}, "
            f"elapsed={int(time.time()-start_time)}s"
        )
        return saved
