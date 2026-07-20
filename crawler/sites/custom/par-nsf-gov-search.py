# -*- coding: utf-8 -*-
"""NSF Public Access Repository — Journal Articles crawler.

Target  : https://par.nsf.gov/search/product-type:Journal%20Article
Strategy: Listing-based (no per-item detail-page fetches needed).
  All fields — title, authors, abstract, DOI, date — are embedded in
  each listing <li> block. CSS -webkit-line-clamp only clips the visual
  rendering; the full abstract text is always present in the HTML.

Pagination: path params work for Journal Articles:
  GET https://par.nsf.gov/search/product-type:Journal%20Article/rows:10/page:N
  The breadcrumb carries "Page X of Y"; walk until limit hit, 0 new records,
  or the 200-page safety cap is reached.
  Total corpus: ~278 000 Journal Article records as of May 2026.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    )
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS
except ImportError:
    raise ImportError(
        "beautifulsoup4 is required: pip install beautifulsoup4 html5lib lxml"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SEARCH_BASE   = "https://par.nsf.gov/search/product-type:Journal%20Article"
_ROWS_PER_PAGE = 10           # keep default; server always returns 10 for articles
_MIN_ABSTRACT  = 50           # chars — below this we skip and log
_MAX_PAGES     = 200          # safety cap
_MAX_SECONDS   = 24 * 60      # 24 min (spec: 25 min)
_BACKOFF       = (1, 3, 9)    # exponential-backoff delays in seconds

# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return raw.decode("latin-1", errors="replace")


def _make_soup(raw: bytes | str):
    """Try html5lib → lxml → html.parser; return BeautifulSoup or None."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BS(raw, parser)
        except Exception:
            continue
    return None


def _curl(url: str, retries: int = 3) -> str | None:
    """GET via curl with exponential-backoff retries; returns decoded HTML or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30", "-L",
        "-A",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=45)
            text = _decode(r.stdout)
            if text.strip():
                return text
            print(
                f"[par-nsf-gov-search] empty response "
                f"(attempt {attempt + 1}/{retries}) {url}"
            )
        except Exception as exc:
            print(
                f"[par-nsf-gov-search] curl error "
                f"(attempt {attempt + 1}/{retries}): {exc}"
            )
        if attempt < retries - 1:
            time.sleep(_BACKOFF[min(attempt, len(_BACKOFF) - 1)])
    return None


# ---------------------------------------------------------------------------
# Listing-page parser
# ---------------------------------------------------------------------------

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05",     "june": "06",     "july": "07",  "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _parse_listing(html: str) -> tuple[list[dict], int | None]:
    """
    Parse one search-result listing page.

    Returns (items, total_pages).  Each item dict contains:
      external_id, title, url, doi, authors, published_date,
      publisher, journal, abstract.
    total_pages is None if the breadcrumb indicator is absent.
    """
    total_pages: int | None = None
    m = re.search(r"Page \d+ of (\d+)", html)
    if m:
        total_pages = int(m.group(1))

    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[par-nsf-gov-search] BeautifulSoup error: {exc}")
        return [], total_pages
    if soup is None:
        return [], total_pages

    items: list[dict] = []
    for li in soup.find_all("li"):
        try:
            article = li.select_one("div.article.item.document")
            if not article:
                continue

            # Title and canonical biblio URL
            title_el = article.find(itemprop="name")
            url_el   = article.find(itemprop="url")
            if not title_el or not url_el:
                continue

            title      = title_el.get_text(strip=True)
            biblio_url = (url_el.get("href") or "").strip()
            if not title or not biblio_url:
                continue

            # External ID: numeric OSTI/PAR ID embedded in the biblio path
            mid = re.search(r"/biblio/(\d+)", biblio_url)
            external_id = mid.group(1) if mid else ""

            # PDF URL: NSF PAR uses /servlets/purl/<ID> for open-access PDFs
            pdf_url = (
                f"https://par.nsf.gov/servlets/purl/{external_id}"
                if external_id else ""
            )

            # DOI
            doi = ""
            doi_a = article.find("a", href=re.compile(r"doi\.org/", re.I))
            if doi_a:
                mdi = re.search(r"doi\.org/(.+)", doi_a.get("href", ""), re.I)
                if mdi:
                    doi = mdi.group(1).strip()

            # Authors
            authors = [
                el.get_text(strip=True)
                for el in article.find_all(itemprop="author")
                if el.get_text(strip=True)
            ]

            # Publication date — prefer datetime="" attribute (YYYY-MM-DD);
            # fall back to human-readable text "Month YYYY".
            published_date = ""
            date_el = article.find("time", itemprop="datePublished")
            if date_el:
                dt = (date_el.get("datetime") or "").strip()
                if re.match(r"\d{4}-\d{2}-\d{2}", dt):
                    published_date = dt
                else:
                    txt = date_el.get_text(strip=True)
                    m2 = re.match(r"([A-Za-z]+)\s+(\d{4})", txt)
                    if m2:
                        mo = _MONTHS.get(m2.group(1).lower(), "01")
                        published_date = f"{m2.group(2)}-{mo}-01"

            # Journal name — often in a <a class="misc external-link"> after authors
            journal = ""
            misc_a = article.find("a", class_=re.compile(r"misc"))
            if misc_a:
                journal = misc_a.get_text(strip=True)

            # Publisher / year block
            publisher = ""
            year_el = article.find("span", class_="year")
            if year_el:
                yr_txt = year_el.get_text(" ", strip=True)
                mp = re.search(r"\([^,]+,\s*(.+?)\)", yr_txt)
                if mp:
                    publisher = mp.group(1).strip()

            # Abstract — itemprop="description" div.  CSS line-clamp only clips
            # the visual rendering; the full text is always in the HTML.
            abstract_el = article.find(itemprop="description")
            abstract = abstract_el.get_text(" ", strip=True) if abstract_el else ""
            # Strip leading "Abstract " prefix if present
            abstract = re.sub(r"^Abstract\s+", "", abstract).strip()

            items.append({
                "external_id":    external_id,
                "title":          title,
                "url":            biblio_url,
                "pdf_url":        pdf_url,
                "doi":            doi,
                "authors":        authors,
                "published_date": published_date,
                "publisher":      publisher,
                "journal":        journal,
                "abstract":       abstract,
            })

        except Exception as exc:
            print(f"[par-nsf-gov-search] item parse error: {exc}")
            continue

    return items, total_pages


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class PARNSFGovSearchCrawler(BaseCrawler):
    """NSF PAR – Journal Articles (https://par.nsf.gov)."""

    site_id   = "par-nsf-gov-search"
    site_name = "Custom: par-nsf-gov-search"
    base_url  = "https://par.nsf.gov"

    def crawl(self, limit=None):  # noqa: C901
        """
        Crawl NSF PAR Journal Articles and persist records to the DB.

        Parameters
        ----------
        limit : int | None
            Maximum records to save.  None = unlimited.

        Returns
        -------
        int
            Number of records saved in this run.
        """
        saved      = 0
        seen_urls: set = set()
        limit_str  = str(limit) if limit is not None else "inf"
        start_time = time.time()
        page       = 1

        print(f"[par-nsf-gov-search] Starting Journal Article crawl (limit={limit_str})")

        while True:
            try:
                # ── wall-clock budget ──────────────────────────────────────
                elapsed = time.time() - start_time
                if elapsed > _MAX_SECONDS:
                    print(
                        f"[par-nsf-gov-search] Approaching 25-min budget "
                        f"({elapsed:.0f}s elapsed). Stopping cleanly."
                    )
                    break

                if limit is not None and saved >= limit:
                    break

                if page > _MAX_PAGES:
                    print(
                        f"[par-nsf-gov-search] Safety cap of {_MAX_PAGES} pages reached. "
                        f"Logging and stopping."
                    )
                    break

                # ── fetch listing page ─────────────────────────────────────
                url  = (
                    f"{_SEARCH_BASE}"
                    f"/rows:{_ROWS_PER_PAGE}"
                    f"/page:{page}"
                )
                html = _curl(url)
                if not html:
                    print(f"[par-nsf-gov-search] Failed to fetch page {page}. Stopping.")
                    break

                items, total_pages = _parse_listing(html)
                if page == 1 and total_pages is not None:
                    print(
                        f"[par-nsf-gov-search] "
                        f"Total listing pages: {total_pages} "
                        f"(≈{total_pages * _ROWS_PER_PAGE:,} records)"
                    )

                if not items:
                    print(f"[par-nsf-gov-search] No items on page {page}. Done.")
                    break

                # Progress log every 10 pages
                if page % 10 == 0:
                    print(
                        f"[par-nsf-gov-search] page {page}: saved {saved}/{limit_str}"
                    )

                # ── process items on this page ─────────────────────────────
                new_on_page = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    item_url = item.get("url", "")
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_on_page += 1

                    try:
                        abstract = item.get("abstract", "")
                        if len(abstract) < _MIN_ABSTRACT:
                            print(
                                f"[par-nsf-gov-search] "
                                f"{item.get('external_id', '?')}: abstract too short "
                                f"({len(abstract)} chars), skipping"
                            )
                            continue

                        self._save_paper({
                            "id":             None,
                            "site_id":        self.site_id,
                            "external_id":    item["external_id"],
                            "title":          item["title"],
                            "authors":        json.dumps(
                                                  item["authors"], ensure_ascii=False
                                              ),
                            "abstract":       abstract,
                            "category":       "Journal Article",
                            "keywords":       json.dumps([], ensure_ascii=False),
                            "published_date": item["published_date"],
                            "url":            item["url"],
                            "pdf_url":        item["pdf_url"],
                            "doi":            item["doi"],
                            "department":     "",
                            "metadata":       json.dumps(
                                                  {
                                                      "journal":   item.get("journal", ""),
                                                      "publisher": item.get("publisher", ""),
                                                  },
                                                  ensure_ascii=False,
                                              ),
                        })
                        saved += 1
                        print(
                            f"[par-nsf-gov-search] Saved {saved}/{limit_str}: "
                            f"{item['title'][:60]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[par-nsf-gov-search] item "
                            f"{item.get('external_id', '?')} failed: {exc}"
                        )
                        continue

                if new_on_page == 0:
                    print(
                        f"[par-nsf-gov-search] "
                        f"No new items on page {page} (all duplicates). Done."
                    )
                    break

                page += 1
                time.sleep(self._delay)

            except KeyboardInterrupt:
                print(f"\n[par-nsf-gov-search] Interrupted. Saved {saved} so far.")
                raise

        print(f"[par-nsf-gov-search] Done. Total saved: {saved}")
        return saved
