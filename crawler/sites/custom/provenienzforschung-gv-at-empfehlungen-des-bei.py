# -*- coding: utf-8 -*-
"""Crawler for provenienzforschung.gv.at - Kunstrückgabebeirat Beschlüsse (advisory board decisions)."""

import io
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

# Absolute import — spec_from_file_location has no package context.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler

_SITE_ID = "provenienzforschung-gv-at-empfehlungen-des-bei"
_LIST_URL = (
    "https://provenienzforschung.gv.at"
    "/empfehlungen-des-beirats/beschluesse/beschluesse-alphabetisch/"
)
_BASE_URL = "https://provenienzforschung.gv.at"
_PUBLISHER = "Kunstrückgabebeirat"


def _curl_get(url: str, binary: bool = False):
    """GET via curl with up to 3 retries (1s, 3s, 9s backoff). Returns bytes or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL",
        "--max-time", "60",
        "-A", BaseCrawler.USER_AGENT,
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=65)
            if result.returncode == 0 and result.stdout:
                return result.stdout
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}: {exc}")
        if attempt < 2:
            wait = (attempt + 1) * 3  # 3s, 9s
            print(f"[{_SITE_ID}] curl failed attempt {attempt + 1}, retrying in {wait}s...")
            time.sleep(wait)
    return None


def _bs4_parse(raw: bytes):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _extract_pdf_text(pdf_bytes: bytes) -> str:
    """Extract all text from PDF bytes using pdfplumber. Returns '' on failure."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = []
            for page in pdf.pages:
                try:
                    text = page.extract_text()
                    if text:
                        parts.append(text.strip())
                except Exception:
                    continue
            return "\n\n".join(parts)
    except Exception as exc:
        print(f"[{_SITE_ID}] PDF text extraction failed: {exc}")
        return ""


def _date_from_text(text: str) -> str:
    """Extract first YYYY-MM-DD pattern from text."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else ""


class ProvenienzforschungBeschluesseCrawler(BaseCrawler):
    """Crawler for Austrian art restitution advisory board decisions (Beschlüsse)."""

    site_id = "provenienzforschung-gv-at-empfehlungen-des-bei"
    site_name = "Custom: provenienzforschung-gv-at-empfehlungen-des-bei"
    base_url = "https://provenienzforschung.gv.at"

    def crawl(self, limit=None):
        """Crawl the alphabetical Beschlüsse list and save each PDF decision.

        All 497 decisions are on a single HTML page — no multi-page
        pagination. The PDF is the primary content source; its extracted
        text becomes the abstract.
        """
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60

        limit_str = str(limit) if limit is not None else "∞"

        # ── 1. Fetch list page ──────────────────────────────────────────
        print(f"[{self.site_id}] Fetching list: {_LIST_URL}")
        raw = _curl_get(_LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch list page. Aborting.")
            return 0

        raw_text = raw.decode("utf-8", errors="replace")
        soup = _bs4_parse(raw_text.encode("utf-8"))
        if not soup:
            print(f"[{self.site_id}] Failed to parse HTML. Aborting.")
            return 0

        table = soup.find("table", class_="downloads")
        if not table:
            print(f"[{self.site_id}] <table class='downloads'> not found. Aborting.")
            return 0

        rows = table.find_all("tr")
        print(f"[{self.site_id}] Found {len(rows)} rows. limit={limit_str}")

        saved = 0
        seen_urls: set = set()
        SAFETY_PAGE_CAP = 200  # nominal cap; single-page site won't reach it
        page_equiv = 0         # tracks logical "pages" for the progress log

        for row_idx, row in enumerate(rows):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached at row {row_idx}. Stopping.")
                break

            if row_idx > 0 and row_idx % 10 == 0:
                page_equiv = row_idx // 10
                if page_equiv >= SAFETY_PAGE_CAP:
                    print(f"[{self.site_id}] Safety cap {SAFETY_PAGE_CAP} pages reached. Stopping.")
                    break
                print(
                    f"[{self.site_id}] page {page_equiv}: saved {saved}/{limit_str}"
                )

            try:
                th = row.find("th")
                a_tag = row.find("a", attrs={"download": True})
                if not th or not a_tag:
                    continue

                th_text = th.get_text(strip=True)
                pdf_href = (a_tag.get("href") or "").strip()
                if not pdf_href:
                    continue

                pdf_url = urljoin(self.base_url, pdf_href)
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                # ── Parse title / date / filename ──
                title = th_text
                published_date = _date_from_text(title)

                pdf_filename = Path(pdf_href.rstrip()).name  # e.g. Abeles_Richard_2004-01-27.pdf
                external_id = Path(pdf_filename).stem        # Abeles_Richard_2004-01-27

                lang = "en" if (
                    "englisch" in title.lower() or "_englisch" in pdf_href.lower()
                ) else "de"

                # ── 2. Download PDF ──────────────────────────────────────
                time.sleep(self._delay)
                pdf_bytes = _curl_get(pdf_url)
                if not pdf_bytes:
                    print(
                        f"[{self.site_id}] item {row_idx} PDF download failed, skipping: {pdf_url}"
                    )
                    continue

                # ── 3. Extract text ──────────────────────────────────────
                abstract = _extract_pdf_text(pdf_bytes)
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] item {row_idx} abstract too short "
                        f"({len(abstract)} chars), skipping: {title[:60]}"
                    )
                    continue

                # ── 4. Save ──────────────────────────────────────────────
                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": external_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "posted_date": published_date,
                    "authors": "",
                    "publisher": _PUBLISHER,
                    "department": "",
                    "journal": "",
                    "url": pdf_url,
                    "pdf_url": pdf_url,
                    "keywords": "",
                    "category": "Beschluss",
                    "doi": "",
                    "original_filename": pdf_filename,
                    "metadata": json.dumps(
                        {
                            "posted_date": published_date,
                            "originalFilename": pdf_filename,
                            "language": lang,
                            "table_heading": th_text,
                            "pdf_href": pdf_href,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {row_idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
