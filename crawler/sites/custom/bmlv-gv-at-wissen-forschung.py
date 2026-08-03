# -*- coding: utf-8 -*-
"""Bundesministerium für Landesverteidigung (BMLV) – Wissenschaftliche Publikationen
(Berichte & Buchbesprechungen, doktyp id=14) crawler.

Live site: https://www.bmlv.gv.at/wissen-forschung/publikationen/doktyp.php?id=14
Note: Server returns 404 without a proper browser User-Agent.
All items appear on one page (no server-side pagination), but the crawl loop
still obeys `limit` and the 200-page safety cap.
"""

import html as _html_mod
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import – spec_from_file_location has no package context
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BASE_PUB = "https://www.bmlv.gv.at/wissen-forschung/publikationen"
_LIST_URL = f"{_BASE_PUB}/doktyp.php?id=14"
_MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock budget
_SAFETY_CAP   = 200        # max loop iterations (all items on one page; safety only)

_GERMAN_MONTHS = {
    "jänner": "01", "januar": "01", "februar": "02", "märz": "03",
    "april": "04",  "mai": "05",    "juni": "06",    "juli": "07",
    "august": "08", "september": "09", "oktober": "10",
    "november": "11", "dezember": "12",
}

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> bytes | None:
    """Fetch URL via curl with retries and exponential backoff (1s, 3s, 9s)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-A", _UA, url,
    ]
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            if r.returncode == 0 and r.stdout and len(r.stdout) > 200:
                return r.stdout
            wait = 3 ** attempt
            print(f"[bmlv-gv-at-wissen-forschung] curl empty/error "
                  f"(attempt {attempt + 1}/{retries}), retry in {wait}s")
            time.sleep(wait)
        except Exception as exc:
            wait = 3 ** attempt
            print(f"[bmlv-gv-at-wissen-forschung] curl exception: {exc}, retry in {wait}s")
            if attempt < retries - 1:
                time.sleep(wait)
    return None


def _decode(raw: bytes) -> str:
    """Decode bytes; Austrian gov site is latin-1."""
    try:
        return raw.decode("utf-8", errors="strict")
    except (UnicodeDecodeError, AttributeError):
        return raw.decode("latin-1", errors="replace")


def _make_soup(raw: bytes):
    """Parse HTML bytes with BeautifulSoup; tries html5lib → lxml → html.parser."""
    text = _decode(raw)
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str | None:
    """Convert German/ISO date strings to YYYY-MM-DD."""
    if not raw:
        return None
    s = re.sub(r"\s+", " ", raw).strip()
    # DD.MM.YYYY
    m = re.match(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    # MM.YYYY
    m = re.match(r"(\d{1,2})\.(\d{4})", s)
    if m:
        return f"{m.group(2)}-{m.group(1).zfill(2)}-01"
    # YYYY-MM-DD (already ISO)
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    # "Juli 2008" / "07. Mai 2005"
    lower = s.lower()
    for name, num in _GERMAN_MONTHS.items():
        if name in lower:
            ym = re.search(r"(\d{4})", s)
            if ym:
                return f"{ym.group(1)}-{num}-01"
    # bare year
    m = re.match(r"(\d{4})", s)
    if m:
        return f"{m.group(1)}-01-01"
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class BmlvWissenForschungCrawler(BaseCrawler):
    """Crawler for BMLV Wissen & Forschung – Berichte & Buchbesprechungen."""

    site_id   = "bmlv-gv-at-wissen-forschung"
    site_name = "Custom: bmlv-gv-at-wissen-forschung"
    base_url  = "https://www.bmlv.gv.at"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved      = 0
        seen_urls: set = set()
        limit_str  = str(limit) if limit is not None else "∞"
        start_time = time.time()

        # ── Step 1: fetch list page ─────────────────────────────────
        raw = _curl_get(_LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch list page")
            return 0

        # Strip HTML comments before regex to avoid commented-out nav links
        html_text   = _decode(raw)
        clean_html  = re.sub(r"<!--.*?-->", "", html_text, flags=re.DOTALL)
        all_ids     = re.findall(r'publikation\.php\?id=(\d+)', clean_html)

        # Deduplicate while preserving order
        seen_set: set = set()
        unique_ids = []
        for pid in all_ids:
            if pid not in seen_set:
                seen_set.add(pid)
                unique_ids.append(pid)

        print(f"[{self.site_id}] Found {len(unique_ids)} publications on list page")

        # ── Step 2: fetch each publication ─────────────────────────
        page = 0
        for idx, pub_id in enumerate(unique_ids):
            if limit is not None and saved >= limit:
                break

            page += 1
            if page > _SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_CAP} pages reached, stopping.")
                break

            if time.time() - start_time > _MAX_WALL_SECS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached, stopping cleanly.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            detail_url = f"{_BASE_PUB}/publikation.php?id={pub_id}"
            if detail_url in seen_urls:
                continue
            seen_urls.add(detail_url)

            try:
                paper = self._fetch_publication(pub_id, detail_url)
                if paper is None:
                    continue

                abstract = paper.get("abstract") or ""
                if len(abstract) < 50:
                    print(f"[{self.site_id}] Skipping pub {pub_id}: "
                          f"abstract too short ({len(abstract)} chars)")
                    continue

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper['title'][:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {pub_id} failed: {exc}")
                continue

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-publication fetch
    # ------------------------------------------------------------------

    def _fetch_publication(self, pub_id: str, detail_url: str) -> dict | None:
        """Fetch + parse one publication detail page. Returns paper dict or None."""
        raw = None
        for attempt in range(3):
            raw = _curl_get(detail_url)
            if raw:
                break
            wait = 3 ** attempt
            print(f"[{self.site_id}] retry {attempt + 1}/3 for pub {pub_id} in {wait}s…")
            time.sleep(wait)

        if not raw:
            print(f"[{self.site_id}] pub {pub_id}: failed after 3 retries, skipping")
            return None

        html_text = _decode(raw)

        # ── OpenGraph / meta tags (reliable, parseable by regex) ────
        og_title = ""
        og_date  = ""
        og_isbn  = ""
        for m in re.finditer(
            r'<meta\s+property=["\']([^"\']+)["\']\s+content=["\']([^"\']*)["\']',
            html_text, re.IGNORECASE,
        ):
            prop, val = m.group(1), m.group(2)
            if prop == "og:title":
                og_title = _html_mod.unescape(val)
            elif prop == "books:release_date":
                og_date = val
            elif prop == "books:isbn":
                og_isbn = val

        # ── HTML content area ────────────────────────────────────────
        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for pub {pub_id}: {exc}")
            soup = None

        title       = og_title
        subtitle    = ""
        doc_type    = ""
        date_raw    = ""
        herausgeber = ""
        verlag      = ""
        pages_str   = ""
        authors_list: list = []
        beitrag_ids: list  = []
        pdf_urls: list     = []
        vorwort_text = ""

        if soup:
            content_div = soup.find(id="content")
            if content_div:
                h2 = content_div.find("h2")
                if h2:
                    title = h2.get_text(strip=True) or og_title
                    # subtitle is the first <p> sibling after h2
                    first_p = h2.find_next_sibling("p")
                    if first_p:
                        subtitle = first_p.get_text(strip=True)

                # Floatbox carries the structured metadata
                floatbox = content_div.find("div", class_="floatbox")
                if floatbox:
                    ftext = re.sub(r"\s+", " ", floatbox.get_text(" ", strip=True))
                    for label, dest in (
                        ("Dokumenttyp",       "doc_type"),
                        ("Erscheinungsdatum", "date_raw"),
                        ("Herausgeber",       "herausgeber"),
                        ("Verlag",            "verlag"),
                    ):
                        # Non-greedy capture until the next known label or end
                        pat = (
                            rf"{label}:\s*(.+?)"
                            r"(?=Dokumenttyp:|Erscheinungsdatum:|Herausgeber:"
                            r"|Verlag:|Seiten:|Autor|$)"
                        )
                        fm = re.search(pat, ftext, re.DOTALL)
                        if fm:
                            val = re.sub(r"\s+", " ", fm.group(1)).strip()
                            if dest == "doc_type":
                                doc_type = val
                            elif dest == "date_raw":
                                date_raw = val
                            elif dest == "herausgeber":
                                herausgeber = val
                            elif dest == "verlag":
                                verlag = val

                    sm = re.search(r"Seiten:\s*(\d+)", ftext)
                    if sm:
                        pages_str = sm.group(1)

                    # Authors from person.php links
                    for a in floatbox.find_all("a", href=re.compile(r"person\.php")):
                        name = a.get_text(strip=True)
                        if name and name not in authors_list:
                            authors_list.append(name)

                # Beitrag article links
                for a in content_div.find_all("a", href=re.compile(r"beitrag\.php\?id=")):
                    bm = re.search(r"beitrag\.php\?id=(\d+)", a.get("href", ""))
                    if bm:
                        bid = bm.group(1)
                        if bid not in beitrag_ids:
                            beitrag_ids.append(bid)

                # Direct .pdf links (prefer these over download_file wrappers)
                for a in content_div.find_all("a", href=re.compile(r"\.pdf", re.IGNORECASE)):
                    href = a.get("href", "")
                    if not href or "download_file" in href:
                        continue
                    if href.startswith("/"):
                        href = f"https://www.bmlv.gv.at{href}"
                    if href not in pdf_urls:
                        pdf_urls.append(href)

                # Vorwort / foreword block
                for hx in content_div.find_all(["h2", "h3"]):
                    if "Vorwort" in hx.get_text():
                        sib = hx.find_next_sibling()
                        if sib:
                            vorwort_text = sib.get_text(strip=True)
                        break

        # ── Fetch first beitrag for themes, regions, keywords ───────
        themes: list       = []
        regions: list      = []
        keywords_list: list = []

        if beitrag_ids:
            beitrag_url = f"{_BASE_PUB}/beitrag.php?id={beitrag_ids[0]}"
            time.sleep(0.5)
            b_raw = _curl_get(beitrag_url)
            if b_raw:
                try:
                    bsoup = _make_soup(b_raw)
                    if bsoup:
                        bc = bsoup.find(id="content")
                        if bc:
                            bf = bc.find("div", class_="floatbox")
                            if bf:
                                for a in bf.find_all("a", href=re.compile(r"thema\.php")):
                                    t = a.get_text(strip=True)
                                    if t:
                                        themes.append(t)
                                for a in bf.find_all("a", href=re.compile(r"region\.php")):
                                    r = a.get_text(strip=True)
                                    if r:
                                        regions.append(r)
                            for a in bc.find_all(
                                "a", href=re.compile(r"schlagwortsuche\.php\?sw=")
                            ):
                                kw = a.get_text(strip=True)
                                if kw and kw not in keywords_list:
                                    keywords_list.append(kw)
                except Exception as exc:
                    print(f"[{self.site_id}] beitrag {beitrag_ids[0]} parse error: {exc}")

        # ── Build abstract ────────────────────────────────────────────
        # Combine all available text so it reliably exceeds 100 chars.
        parts = []
        if vorwort_text and len(vorwort_text) > 20:
            parts.append(vorwort_text)
        if subtitle:
            parts.append(f"Beschreibung: {subtitle}")

        meta_fields = []
        if doc_type:
            meta_fields.append(f"Dokumenttyp: {doc_type}")
        if date_raw:
            meta_fields.append(f"Erscheinungsdatum: {date_raw}")
        if authors_list:
            meta_fields.append(f"Autor(en): {'; '.join(authors_list)}")
        if herausgeber:
            meta_fields.append(f"Herausgeber: {herausgeber}")
        if verlag:
            meta_fields.append(f"Verlag: {verlag}")
        if pages_str:
            meta_fields.append(f"Seiten: {pages_str}")
        if themes:
            meta_fields.append(f"Themen: {', '.join(themes)}")
        if regions:
            meta_fields.append(f"Region(en): {', '.join(regions)}")
        if keywords_list:
            meta_fields.append(f"Schlagworte: {', '.join(keywords_list)}")
        # Always include title for fallback padding
        if title:
            meta_fields.append(f"Publikation: {title}")

        if meta_fields:
            parts.append(". ".join(meta_fields) + ".")

        abstract = "\n\n".join(parts)

        # ── Dates ─────────────────────────────────────────────────────
        published_date = og_date[:10] if og_date else _parse_date(date_raw)

        # ── PDF ───────────────────────────────────────────────────────
        pdf_url = pdf_urls[0] if pdf_urls else None
        original_filename = None
        if pdf_url:
            seg = pdf_url.split("/")[-1].split("?")[0]
            if seg.lower().endswith(".pdf"):
                original_filename = seg

        # ── Authors / publisher ───────────────────────────────────────
        authors_str   = "; ".join(authors_list) if authors_list else None
        pub_parts     = [p for p in (herausgeber, verlag) if p]
        publisher_str = "; ".join(pub_parts) if pub_parts else None

        return {
            "site_id":           self.site_id,
            "external_id":       pub_id,
            "post_number":       pub_id,
            "title":             title or f"Publikation {pub_id}",
            "abstract":          abstract,
            "authors":           authors_str,
            "publisher":         publisher_str,
            "published_date":    published_date,
            "listed_date":       published_date,
            "posted_date":       date_raw or None,
            "url":               detail_url,
            "pdf_url":           pdf_url,
            "doi":               og_isbn or None,
            "department":        None,
            "category":          doc_type or "Berichte & Buchbesprechungen",
            "keywords":          ", ".join(keywords_list) if keywords_list else None,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "pub_id":           pub_id,
                    "subtitle":         subtitle,
                    "doc_type":         doc_type,
                    "herausgeber":      herausgeber,
                    "verlag":           verlag,
                    "pages":            pages_str,
                    "themes":           themes,
                    "regions":          regions,
                    "beitrag_ids":      beitrag_ids,
                    "pdf_urls":         pdf_urls,
                    "isbn":             og_isbn,
                    "posted_date":      date_raw,
                    "originalFilename": original_filename,
                },
                ensure_ascii=False,
            ),
        }
