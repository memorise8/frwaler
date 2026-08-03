# -*- coding: utf-8 -*-
"""BiOrbic (biorbic.com) publications crawler.

Starting page:
    https://biorbic.com/publications/

The page is a WordPress / Beaver Builder site with no REST-exposed
``publication`` post type (``/wp-json/wp/v2/types`` only lists ``post``,
``page``, ``committee``, ``projects``, ``researcher``, ``resource``, ...).
The publication list itself is rendered by the "Ninja Tables" plugin
(``#footable_41203``). Its client-side JS *does* expose a
``wp_ajax_ninja_tables_public_action`` AJAX endpoint, but that endpoint
requires a per-page nonce that is only wired up for the search/filter UI —
calling it directly (with or without a nonce) returns an empty
``{"success":true,"data":[]}``. Verified with curl: **all 587 rows are
already server-side rendered into the single ``/publications/`` HTML
response** — Ninja Tables' JS only paginates/filters rows that are already
in the DOM. So there is exactly one real HTTP request for the whole
catalogue; the crawl loop below chunks that in-memory row list into
"pages" purely to satisfy the standard progress-logging / safety-cap
contract, not because further network calls happen.

Table columns (in order): Journal, Date, Challenge, Authors, Type, Title,
Status, DOI, Open Access?, Keywords. There is no abstract text anywhere on
the site for these rows, so (matching the convention already used by
``waltoninstitute-ie-research.py`` for the same situation) we build a
bibliographic abstract from the row's own fields rather than attempting to
scrape abstracts from ~500 distinct external publisher domains (DOI
targets), which would be slow, unreliable, and frequently blocked/paywalled.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from html import unescape

# Absolute import: spec_from_file_location has no package context.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


SITE_ID = "biorbic-com-publications"
BASE_URL = "https://biorbic.com"
LIST_URL = "https://biorbic.com/publications/"

SAFETY_CAP_PAGES = 200
WALL_CLOCK_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
RETRY_WAITS = (1, 3, 9)
MIN_ABSTRACT_CHARS = 50
PAGE_CHUNK = 25  # rows per "virtual page" for progress logging only

_DOI_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>]+", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_BS_PARSER_CACHE: list = [None]

# Single-shot cp1252<->utf-8 round-tripping fixes a clean double-encoding,
# but strings with a *second*, unrelated corruption (e.g. a mangled NBSP)
# fail the round-trip as a whole. These are the handful of byte-sequences
# that account for nearly every occurrence seen in this site's author list.
_MOJIBAKE_FALLBACK_MAP = {
    "â€™": "’", "â€˜": "‘",
    "â€œ": "“", "â€\x9d": "”",
    "â€“": "–", "â€”": "—", "â€¦": "…",
    "Ã¤": "ä", "Ã¶": "ö", "Ã¼": "ü", "Ã©": "é", "Ã¨": "è",
    "Ã¡": "á", "Ã³": "ó", "Ã±": "ñ", "Ã§": "ç", "Ã¯": "ï",
    "Ã¸": "ø", "Ã¥": "å", "Ã­": "í", "Ãº": "ú",
}


def _make_soup(raw: str):
    """Build BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[{SITE_ID}] BeautifulSoup unavailable: {exc}")
        return None

    parsers = ["html5lib", "lxml", "html.parser"]
    cached = _BS_PARSER_CACHE[0]
    if cached:
        parsers = [cached] + [p for p in parsers if p != cached]

    for parser in parsers:
        try:
            soup = BeautifulSoup(raw or "", parser)
            _BS_PARSER_CACHE[0] = parser
            return soup
        except Exception as exc:
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
    return None


def _fix_mojibake(text: str) -> str:
    """Repair UTF-8-bytes-mis-decoded-as-cp1252 double-encoding artifacts.

    The Ninja Tables data on this site contains names like
    ``Yurii K Gunâ€™ko`` / ``JÃ¼rgen`` — real UTF-8 text (', u, ...) that got
    decoded once as windows-1252 and re-saved as UTF-8. Round-tripping
    through cp1252 -> utf-8 undoes it; if that fails (not actually
    mojibake, or a different corruption), the original text is kept as-is.

    Must run BEFORE ``\\xa0`` (non-breaking space) gets collapsed to a plain
    space: a mojibake'd NBSP decodes as the two-byte sequence ``Â\\xa0``, and
    once ``\\xa0`` is flattened to a normal space the trailing byte needed
    for the utf-8 round-trip is gone, breaking the repair for any string
    that also contains a NBSP.
    """
    if not text or not any(marker in text for marker in ("Ã", "â€", "Â")):
        return text
    try:
        fixed = text.encode("cp1252").decode("utf-8")
        if "�" not in fixed:
            return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        pass

    # Whole-string round-trip failed — usually means only *part* of the
    # string is double-encoded (e.g. a lost/mangled NBSP elsewhere breaks
    # the byte alignment). Fall back to patching the handful of mojibake
    # byte-sequences that account for almost all real-world occurrences,
    # and drop orphaned "Â" markers (the lead byte of a mis-decoded NBS
    # whose trailing byte was lost) rather than leaving visible garbage.
    repaired = text
    for bad, good in _MOJIBAKE_FALLBACK_MAP.items():
        repaired = repaired.replace(bad, good)
    repaired = re.sub(r"Â(?=[\s\W]|$)", "", repaired)
    return repaired


def _clean_text(value) -> str:
    if value is None:
        return ""
    text = _fix_mojibake(unescape(str(value)))
    text = text.replace("\xa0", " ").replace("​", "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_ddmmyyyy(raw: str):
    text = _clean_text(raw)
    if not text:
        return None
    match = _DATE_RE.match(text)
    if match:
        day, month, year = match.groups()
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            return None
    year_match = re.search(r"\b(19|20)\d{2}\b", text)
    if year_match:
        return f"{year_match.group(0)}-01-01"
    return None


def _normalise_authors(value: str):
    text = _clean_text(value)
    if not text:
        return None
    text = re.sub(r"\s+\band\b\s+", ", ", text, flags=re.IGNORECASE)
    parts = [p.strip() for p in re.split(r"\s*[,;]\s*", text) if p.strip()]
    seen = set()
    out = []
    for part in parts:
        key = part.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(part)
    return "; ".join(out) if out else None


def _extract_doi(value: str):
    text = _clean_text(value)
    if not text:
        return None
    match = _DOI_RE.search(text)
    if match:
        return match.group(0).rstrip(".,;)")
    return None


def _json_dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class BiorbicComPublicationsCrawler(BaseCrawler):
    site_id = SITE_ID
    site_name = "Custom: biorbic-com-publications"
    base_url = BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Low-level fetch
    # ------------------------------------------------------------------

    def _curl_fetch(self, url: str, context: str, retries: int = 3, timeout: int = 60):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--max-time", str(timeout),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(1, retries + 1):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
                if result.returncode == 0 and result.stdout and result.stdout.strip():
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                print(f"[{self.site_id}] curl {context} attempt {attempt}/{retries} failed: "
                      f"rc={result.returncode} {stderr[:200]}")
            except KeyboardInterrupt:
                raise
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl {context} attempt {attempt}/{retries} timed out")
            except Exception as exc:
                print(f"[{self.site_id}] curl {context} attempt {attempt}/{retries} exception: {exc}")

            if attempt < retries:
                wait = RETRY_WAITS[min(attempt - 1, len(RETRY_WAITS) - 1)]
                time.sleep(wait)

        print(f"[{self.site_id}] curl {context} failed after {retries} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _fetch_rows(self):
        html = self._curl_fetch(LIST_URL, "publications listing")
        if not html:
            return []

        soup = _make_soup(html)
        if soup is None:
            print(f"[{self.site_id}] failed to parse listing HTML with any parser")
            return []

        table = soup.find("table", id="footable_41203")
        if table is None:
            table = soup.find("table", class_=re.compile(r"ninja_footable"))
        if table is None:
            print(f"[{self.site_id}] publications table not found in listing page")
            return []

        tbody = table.find("tbody") or table
        trs = tbody.find_all("tr")
        rows = []
        for i, tr in enumerate(trs):
            try:
                tds = tr.find_all("td")
                if len(tds) < 10:
                    continue
                row_id = tr.get("data-row_id")
                if row_id is None or not str(row_id).strip():
                    row_id = str(i)
                cells = [td.get_text(" ", strip=True) for td in tds[:10]]
                rows.append({
                    "row_id": str(row_id).strip(),
                    "journal": cells[0],
                    "date": cells[1],
                    "challenge": cells[2],
                    "authors": cells[3],
                    "type": cells[4],
                    "title": cells[5],
                    "status": cells[6],
                    "doi_cell": cells[7],
                    "open_access": cells[8],
                    "keywords": cells[9],
                })
            except Exception as exc:
                print(f"[{self.site_id}] failed to parse table row {i}: {exc}")
                continue
        return rows

    def _build_paper(self, row: dict):
        row_id = row["row_id"]

        title = _clean_text(row.get("title"))
        if not title:
            print(f"[{self.site_id}] row {row_id}: empty title, skipping")
            return None

        authors_raw = _clean_text(row.get("authors"))
        authors = _normalise_authors(authors_raw)

        journal_cell = _clean_text(row.get("journal"))
        journal = None if journal_cell.lower() in ("", "not applicable", "n/a", "na") else journal_cell

        date_raw = _clean_text(row.get("date"))
        published_date = _parse_ddmmyyyy(date_raw)

        challenge = _clean_text(row.get("challenge")) or None
        pub_type = _clean_text(row.get("type")) or None
        status = _clean_text(row.get("status")) or None
        open_access = _clean_text(row.get("open_access")) or None
        keywords = _clean_text(row.get("keywords")) or None

        doi_cell = _clean_text(row.get("doi_cell"))
        doi = _extract_doi(doi_cell)
        reference_url = doi_cell if (not doi and doi_cell.lower().startswith("http")) else None

        if doi:
            detail_url = f"https://doi.org/{doi}"
        elif reference_url:
            detail_url = reference_url
        else:
            detail_url = f"{LIST_URL}#row-{row_id}"

        abstract_parts = [
            f"Title: {title}",
            f"Authors: {authors}" if authors else "",
            f"Journal: {journal}" if journal else "",
            f"Publication type: {pub_type}" if pub_type else "",
            f"Published date: {published_date}" if published_date else "",
            f"Research challenge: {challenge}" if challenge else "",
            f"Status: {status}" if status else "",
            f"Open access: {open_access}" if open_access else "",
            f"DOI: {doi}" if doi else "",
            f"Keywords: {keywords}" if keywords else "",
            f"Reference: {detail_url}",
        ]
        abstract = ". ".join(p for p in abstract_parts if p).strip()
        if len(abstract) < MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] row {row_id}: abstract too short ({len(abstract)} chars), skipping")
            return None

        metadata = {
            "posted_date": date_raw,
            "originalFilename": None,
            "journal_raw": journal_cell or None,
            "series": None,
            "volume": None,
            "issue": None,
            "row_id": row_id,
            "challenge": challenge,
            "department": challenge,
            "type_raw": pub_type,
            "status_raw": status,
            "open_access_raw": open_access,
            "doi_cell_raw": doi_cell or None,
            "keywords_raw": keywords,
            "reference_url": detail_url,
        }

        return {
            "id": f"{self.site_id}-{row_id}",
            "site_id": self.site_id,
            "external_id": row_id,
            "post_number": row_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "listed_date": published_date,
            "authors": authors,
            "publisher": "BiOrbic Research Ireland Centre for Bioeconomy",
            "department": challenge,
            "journal": journal,
            "url": detail_url,
            "meta_url": detail_url,
            "pdf_url": None,
            "keywords": keywords,
            "category": pub_type,
            "doi": doi,
            "original_filename": None,
            "metadata": _json_dumps(metadata),
        }

    # ------------------------------------------------------------------
    # Crawl entrypoint
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        if limit is not None:
            try:
                limit = int(limit)
            except (TypeError, ValueError):
                raise ValueError("limit must be an integer or None")
            if limit <= 0:
                return 0

        start = time.monotonic()
        limit_or_inf = limit if limit is not None else "inf"

        rows = self._fetch_rows()
        if not rows:
            print(f"[{self.site_id}] no rows parsed from listing page; nothing to save")
            return 0
        print(f"[{self.site_id}] fetched {len(rows)} publication rows from listing page")

        saved = 0
        seen_urls = set()
        idx = 0
        page = 0

        try:
            while idx < len(rows):
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - start >= WALL_CLOCK_BUDGET_SECONDS - 30:
                    print(f"[{self.site_id}] approaching 25 minute crawl budget; exiting cleanly")
                    break

                page += 1
                if page > SAFETY_CAP_PAGES:
                    print(f"[{self.site_id}] reached safety cap of {SAFETY_CAP_PAGES} pages; stopping")
                    break

                chunk = rows[idx: idx + PAGE_CHUNK]
                idx += PAGE_CHUNK

                for row in chunk:
                    if limit is not None and saved >= limit:
                        break
                    row_id = row.get("row_id")
                    try:
                        paper = self._build_paper(row)
                    except Exception as exc:
                        print(f"[{self.site_id}] item row {row_id} failed: {exc}")
                        continue
                    if paper is None:
                        continue
                    if paper["url"] in seen_urls:
                        continue
                    seen_urls.add(paper["url"])
                    try:
                        self._save_paper(paper)
                    except Exception as exc:
                        print(f"[{self.site_id}] item row {row_id} save failed: {exc}")
                        continue
                    saved += 1

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done: saved {saved}/{limit_or_inf}")
        return saved
