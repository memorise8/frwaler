# -*- coding: utf-8 -*-
"""BMLEH (Federal Ministry of Food, Agriculture and Consumer Protection) crawler.

Source: https://www.bmleh.de/EN/ministry/organisation/advisory-boards/AgriculturalPolicyPublications.html

This is a single static list page (no server-side pagination) with three
kinds of `<li><a>` entries:

  - class="RichTextIntLink Publication FTpdf" -> link to a detail HTML page
    that itself has a "Date" field and a PDF download link.
  - class="RichTextIntLink Basepage"          -> link to a full HTML article
    page (the article body itself is the content, no separate PDF).
  - class="Publication" title="Opens in new window" -> a direct link to a
    PDF, with no detail page at all.

The abstract is extracted from the underlying PDF (pdfplumber, falling back
to pypdf) when one is available, or from the HTML article body otherwise.
"""

from __future__ import annotations

import html as html_lib
import io
import json
import re
import subprocess
import time
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_MONTHS = {
    "january": 1, "januar": 1,
    "february": 2, "februar": 2,
    "march": 3, "märz": 3, "maerz": 3, "marz": 3,
    "april": 4,
    "may": 5, "mai": 5,
    "june": 6, "juni": 6,
    "july": 7, "juli": 7,
    "august": 8,
    "september": 9,
    "october": 10, "oktober": 10,
    "november": 11,
    "december": 12, "dezember": 12,
}

_DATE_RE = re.compile(r"(?:(?P<day>\d{1,2})\s+)?(?P<month>[A-Za-zÄäÖöÜü]+)\s+(?P<year>\d{4})")

_ABSTRACT_HEADERS = (
    r"\bExecutive Summary\b",
    r"\bAbstract\b",
    r"\bSummary\b",
    r"\bZusammenfassung\b",
    r"\bKey messages\b",
)

_SECTION_END_RE = re.compile(
    r"\n\s*(?:\d+[\.\s]+[A-Z]|Contents\b|Introduction\b|Table of [Cc]ontents|References\b|Acknowledg)"
)

_CATEGORY_KEYWORDS = (
    "Executive Summary", "Position Paper", "Joint Statement", "Statement",
    "Report", "Expertise", "Opinion", "Recommendations", "Abstract",
)


def _parse_date(text):
    """Return the LAST recognizable 'Month Year' / 'Day Month Year' date in text, as ISO, else None."""
    if not text:
        return None
    best = None
    for m in _DATE_RE.finditer(text):
        month_word = m.group("month").lower()
        if month_word not in _MONTHS:
            continue
        year = int(m.group("year"))
        if not (1900 <= year <= 2100):
            continue
        month = _MONTHS[month_word]
        day = int(m.group("day")) if m.group("day") else 1
        if not (1 <= day <= 31):
            day = 1
        best = f"{year:04d}-{month:02d}-{day:02d}"
    return best


def _clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def _looks_like_prose(text, min_words=25):
    """Reject table-of-contents dot-leaders / cover-page boilerplate masquerading as a section."""
    if re.search(r"\.{4,}", text):
        return False
    if re.search(r"https?://|www\.", text):
        return False
    words = [w for w in text.split() if len(w) >= 3]
    return len(words) >= min_words


def _extract_abstract(text, min_len=100, max_len=2500):
    """Pull an 'Abstract'/'Executive Summary'-style section out of running text.

    Tries every occurrence of each header (a table of contents entry can look
    like a false-positive header match), skipping ones that don't look like
    real prose. Falls back to the cleaned whole text when no recognizable
    section header yields a usable chunk but there's still enough content.
    """
    if not text:
        return ""
    for pat in _ABSTRACT_HEADERS:
        for mo in re.finditer(pat, text):
            after = text[mo.end():].lstrip(" :\n")
            end = _SECTION_END_RE.search(after)
            chunk = after[:end.start()] if end else after[:max_len]
            cleaned = _clean(chunk)
            if len(cleaned) >= min_len and _looks_like_prose(cleaned):
                return cleaned[:max_len]
    cleaned = _clean(text)
    if len(cleaned) >= min_len:
        return cleaned[:max_len]
    return ""


def _detect_category(text):
    if not text:
        return "Publication"
    low = text.lower()
    for kw in _CATEGORY_KEYWORDS:
        if kw.lower() in low:
            return kw
    return "Publication"


def _filename_from_url(url):
    if not url:
        return None
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    return unquote(name) if name else None


def _slug_from_url(url):
    if not url:
        return None
    name = urlsplit(url).path.rsplit("/", 1)[-1]
    name = name.rsplit(".", 1)[0] if "." in name else name
    name = unquote(name).strip()
    return name or None


def _pdf_text(pdf_bytes, max_pages=6):
    """Extract text from the first ``max_pages`` pages of a PDF."""
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages[:max_pages])
    except Exception:
        pass
    try:
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes), strict=False)
        return "\n".join((p.extract_text() or "") for p in list(reader.pages)[:max_pages])
    except Exception:
        return ""


def _main_content_region(raw_html):
    """Slice out the main article body, dropping nav/footer/related-links noise."""
    idx = raw_html.find('id="content"')
    if idx == -1:
        return raw_html
    region = raw_html[idx:]
    cut_points = [
        p for p in (
            region.find('<p class="c-teaser__meta"'),
            region.find('<div class="l-content-wrapper"'),
            region.find("<footer"),
        )
        if p != -1
    ]
    if cut_points:
        region = region[:min(cut_points)]
    return region


def _find_pdf_link(region, base_url):
    if not region:
        return None
    m = re.search(r'class="c-download-teaser__a"[^>]*href="([^"]+)"', region)
    if not m:
        m = re.search(r'href="([^"]+)"[^>]*class="c-download-teaser__a"', region)
    if not m:
        return None
    href = html_lib.unescape(m.group(1))
    if href.startswith("http"):
        return href
    return base_url.rstrip("/") + "/" + href.lstrip("/")


class BmlehDeEnCrawler(BaseCrawler):
    """Crawler for BMLEH advisory-board publications (bmleh.de)."""

    site_id = "bmleh-de-en"
    site_name = "Custom: bmleh-de-en"
    base_url = "https://www.bmleh.de"

    _LIST_URL = "https://www.bmleh.de/EN/ministry/organisation/advisory-boards/AgriculturalPolicyPublications.html"
    _PUBLISHER = "Federal Ministry of Food, Agriculture and Consumer Protection"
    _MIN_ABSTRACT = 100
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, binary=False, retries=3):
        """GET via curl with exponential-backoff retries. Returns str/bytes or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
            "-A", self.USER_AGENT, url,
        ]
        waits = [1, 3, 9]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=70)
                if result.stdout:
                    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = waits[attempt]
                    print(f"[{self.site_id}] Empty response (attempt {attempt + 1}/{retries}), retry in {wait}s: {url}")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = waits[attempt]
                    print(f"[{self.site_id}] curl error ({exc}), retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    def _parse_list(self, raw):
        """Return a list of {kind, href, title, trailing} dicts from the list page."""
        soup = self._make_soup(raw)
        if not soup:
            return []

        items = []
        for a in soup.find_all("a", href=True):
            classes = set(a.get("class") or [])
            href = a["href"]

            if {"RichTextIntLink", "Publication", "FTpdf"} <= classes:
                kind = "detail_pdf"
            elif {"RichTextIntLink", "Basepage"} <= classes:
                kind = "basepage"
            elif classes == {"Publication"} and a.get("title") == "Opens in new window":
                kind = "direct_pdf"
            else:
                continue

            li = a.find_parent("li")
            full_text = li.get_text(" ", strip=True) if li else a.get_text(" ", strip=True)
            anchor_text = a.get_text(" ", strip=True)
            if anchor_text and full_text.startswith(anchor_text):
                trailing = full_text[len(anchor_text):].strip()
            else:
                trailing = full_text

            abs_href = href if href.startswith("http") else self.base_url.rstrip("/") + "/" + href.lstrip("/")
            items.append({
                "kind": kind,
                "href": abs_href,
                "title": anchor_text or "(untitled)",
                "trailing": trailing,
            })
        return items

    # ------------------------------------------------------------------
    # Per-item document assembly
    # ------------------------------------------------------------------

    def _build_document(self, item):
        kind = item["kind"]
        href = item["href"]
        title = item["title"]
        listed_date = _parse_date(item["trailing"]) or _parse_date(title)

        region = None
        published_date = None
        pdf_url = None

        if kind in ("detail_pdf", "basepage"):
            detail_raw = self._curl(href)
            if detail_raw:
                region = _main_content_region(detail_raw)
                soup = self._make_soup(region) if region else None
                if soup:
                    h1 = soup.find("h1")
                    if h1 and h1.get_text(strip=True):
                        title = h1.get_text(strip=True)
                date_match = re.search(
                    r'<strong class="label">Date</strong>\s*<span class="value">([\d.]+)</span>',
                    region or "",
                )
                if date_match:
                    published_date = date_match.group(1).replace(".", "-")
                if not published_date and soup:
                    published_date = _parse_date(soup.get_text(" ", strip=True))
                pdf_url = _find_pdf_link(region, self.base_url)
        else:  # direct_pdf
            pdf_url = href

        if not published_date:
            published_date = listed_date

        original_filename = _filename_from_url(pdf_url)

        abstract = ""
        if pdf_url:
            time.sleep(self._delay)
            pdf_bytes = self._curl(pdf_url, binary=True)
            if pdf_bytes:
                abstract = _extract_abstract(_pdf_text(pdf_bytes), self._MIN_ABSTRACT)
            else:
                print(f"[{self.site_id}] Failed to download PDF: {pdf_url}")

        if len(abstract) < self._MIN_ABSTRACT and region:
            html_soup = self._make_soup(region)
            html_text = html_soup.get_text(" ", strip=True) if html_soup else ""
            fallback = _extract_abstract(html_text, self._MIN_ABSTRACT)
            if len(fallback) > len(abstract):
                abstract = fallback

        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] Short abstract ({len(abstract)}c) for '{title[:50]}', skipping.")
            return None

        slug = _slug_from_url(pdf_url if kind == "direct_pdf" else href)
        category = _detect_category(item["trailing"])
        meta_url = self._LIST_URL if kind == "direct_pdf" else href

        metadata = {
            "posted_date": listed_date,
            "posted_date_raw": item["trailing"],
            "kind": kind,
        }
        if original_filename:
            metadata["originalFilename"] = original_filename

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title or "(untitled)",
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": "",
            "publisher": self._PUBLISHER,
            "department": None,
            "journal": None,
            "url": meta_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()

        raw = self._curl(self._LIST_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch list page. Aborting.")
            return 0

        items = self._parse_list(raw)
        if not items:
            print(f"[{self.site_id}] No items parsed from list page. Aborting.")
            return 0

        print(f"[{self.site_id}] Found {len(items)} items on the (single) list page")

        page = 1
        new_in_page = 0
        lim_str = str(limit) if limit is not None else "inf"

        try:
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL}s) exceeded. Stopping cleanly.")
                    break

                href = item["href"]
                if href in seen_urls:
                    continue
                seen_urls.add(href)
                new_in_page += 1

                try:
                    doc = self._build_document(item)
                    if doc is None:
                        continue
                    self._save_paper(doc)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {doc['title'][:60]}")
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({href}): {exc}; continuing.")
                    continue

                time.sleep(self._delay)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")
            if new_in_page == 0:
                print(f"[{self.site_id}] Page {page} returned 0 new records — stopping.")
            if page >= self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
