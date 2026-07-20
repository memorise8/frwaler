# -*- coding: utf-8 -*-
"""Crawler for HATVP - Haute Autorité pour la transparence de la vie publique.

Target: https://www.hatvp.fr/actualites-et-publications/
Section: Communiqués (Presse) - filtered from the #communiques section.

Strategy:
- Walk HTML pages at /actualites-et-publications/ and /actualites-et-publications/page/N/
- Parse #communiques section; keep only items tagged "Communiqués"
- Fetch each detail page (/presse/SLUG/) for full abstract and PDF links
- Stop when limit reached, no new items, or 200-page safety cap
"""

from __future__ import annotations

import html as _html
import json
import re
import subprocess
import sys
import time

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SITE_ID = "hatvp-fr-actualites-et-public"
_BASE = "https://www.hatvp.fr"
_LIST_PAGE1 = f"{_BASE}/actualites-et-publications/"
_LIST_PAGED = f"{_BASE}/actualites-et-publications/page/{{n}}/"
_PUBLISHER = "Haute Autorité pour la transparence de la vie publique"
_MAX_PAGES = 200
_RATE = 1.0          # seconds between detail fetches
_MAX_MINUTES = 25    # wall-clock budget per crawl run
_BACKOFF = (1, 3, 9)

_FR_MONTHS = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_french_date(raw: str) -> str | None:
    """'DD Mois YYYY' or 'Publié le DD mois YYYY' → 'YYYY-MM-DD'."""
    if not raw:
        return None
    text = re.sub(r"(?i)publié\s+le\s+", "", raw).strip()
    m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", text)
    if not m:
        return None
    day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
    month = _FR_MONTHS.get(month_str)
    if not month:
        return None
    return f"{year}-{month}-{day.zfill(2)}"


def _strip_tags(html_fragment: str) -> str:
    """Remove HTML tags and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _curl(url: str, retries: int = 3) -> str | None:
    """Fetch URL with curl (TLS-tolerant). Returns decoded string or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                ["curl", "-Lsk", "--max-time", "30", "-A", _UA, url],
                capture_output=True, timeout=40,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl attempt {attempt+1}/{retries} for {url}: {exc}")
        if attempt < retries - 1:
            wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            print(f"[{_SITE_ID}] Retrying in {wait}s...")
            time.sleep(wait)
    return None


def _bs_parse(html: str):
    """Parse HTML with BeautifulSoup, trying parsers in fallback order."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _extract_communiques(html: str) -> list[dict]:
    """Parse #communiques section and return only Communiqués-tagged items."""
    idx = html.find("id=communiques")
    if idx < 0:
        return []

    # Isolate section (communiques section ends before next section sibling)
    section = html[idx: idx + 50000]

    # Each article is wrapped in <li><div class=wrapper>
    li_positions = [m.start() for m in re.finditer(r"<li><div class=wrapper>", section)]
    if not li_positions:
        return []

    items = []
    for i, start in enumerate(li_positions):
        end = li_positions[i + 1] if i + 1 < len(li_positions) else start + 4000
        li_html = section[start: end]

        # Keep only items tagged as Communiqués
        if ">Communiqués<" not in li_html:
            continue

        # URL (unquoted href in this site's HTML)
        url_m = re.search(
            r"href=(https://www\.hatvp\.fr/presse/[^\s>\"']+)", li_html
        )
        if not url_m:
            continue
        url = url_m.group(1).rstrip("/") + "/"

        # Title
        title_m = re.search(
            r"<h4>\s*<a[^>]*>\s*(.*?)\s*</a>\s*</h4>", li_html, re.DOTALL
        )
        title = _strip_tags(title_m.group(1)) if title_m else ""

        # Brief list-page abstract
        abs_m = re.search(r"<div class=text><p><p>(.*?)</p>", li_html, re.DOTALL)
        list_abstract = _strip_tags(abs_m.group(1)) if abs_m else ""

        # Date
        date_m = re.search(r"<time>\s*(.*?)\s*</time>", li_html)
        date_raw = date_m.group(1).strip() if date_m else ""
        published_date = _parse_french_date(date_raw)

        items.append(
            {
                "url": url,
                "title": title,
                "list_abstract": list_abstract,
                "date_raw": date_raw,
                "published_date": published_date,
            }
        )

    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _fetch_detail(url: str, retries: int = 3) -> dict | None:
    """Fetch and parse a /presse/SLUG/ detail page.

    Returns dict with: title, abstract, published_date, pdf_url.
    Returns None on complete failure.
    """
    html = None
    for attempt in range(retries):
        html = _curl(url)
        if html:
            break
        wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
        print(f"[{_SITE_ID}] detail retry {attempt+1}/{retries} for {url} (wait {wait}s)")
        time.sleep(wait)

    if not html:
        return None

    # Locate <article> element
    idx = html.find("<article")
    if idx < 0:
        return None
    article_html = html[idx: idx + 20000]

    # Title from <h1>
    h1_m = re.search(r"<h1>(.*?)</h1>", article_html, re.DOTALL)
    title = _strip_tags(h1_m.group(1)) if h1_m else ""

    # Published date from <time>
    time_m = re.search(r"<time>(.*?)</time>", article_html, re.DOTALL)
    date_raw = time_m.group(1).strip() if time_m else ""
    published_date = _parse_french_date(date_raw)

    # Main content: prefer <div class=content>
    content_m = re.search(
        r'<div class=content>(.*?)(?:</div>\s*</div>|</article)', article_html, re.DOTALL
    )
    if content_m:
        content_html = content_m.group(1)
    else:
        # Fallback: everything after </header>
        hdr_end = article_html.find("</header>")
        content_html = article_html[hdr_end:] if hdr_end >= 0 else article_html

    abstract = _strip_tags(content_html)

    # PDF links (site uses unquoted AND quoted href attributes)
    pdfs: list[str] = []
    pdfs += re.findall(
        r"href=(https://www\.hatvp\.fr/wordpress/wp-content/uploads/[^\s>\"']+\.pdf)",
        article_html,
    )
    pdfs += re.findall(
        r'href=["\']( https://www\.hatvp\.fr/wordpress/wp-content/uploads/[^"\']+\.pdf)["\']',
        article_html,
    )
    # Deduplicate, preserving order
    seen_pdfs: set[str] = set()
    unique_pdfs = []
    for p in pdfs:
        p = p.strip()
        if p and p not in seen_pdfs:
            seen_pdfs.add(p)
            unique_pdfs.append(p)

    pdf_url = unique_pdfs[0] if unique_pdfs else None

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "pdf_url": pdf_url,
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class HatvpFrActualitesCrawler(BaseCrawler):
    """Crawls the Communiqués section of HATVP's actualités-et-publications."""

    site_id = _SITE_ID
    site_name = "Custom: hatvp-fr-actualites-et-public"
    base_url = _BASE

    def crawl(self, limit=None) -> int:  # type: ignore[override]
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        page_n = 1
        empty_streak = 0
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # ── Time budget ──────────────────────────────────────────────
            elapsed_min = (time.time() - start_time) / 60
            if elapsed_min >= _MAX_MINUTES:
                print(
                    f"[{_SITE_ID}] wall-clock budget {_MAX_MINUTES}m reached, stopping."
                )
                break

            # ── Safety page cap ──────────────────────────────────────────
            if page_n > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached, stopping.")
                break

            # ── Limit check ──────────────────────────────────────────────
            if limit is not None and saved >= limit:
                break

            # ── Progress logging ─────────────────────────────────────────
            if page_n == 1 or page_n % 10 == 0:
                print(
                    f"[{_SITE_ID}] page {page_n}: saved {saved}/{limit_str}"
                )

            # ── Build list URL ───────────────────────────────────────────
            list_url = _LIST_PAGE1 if page_n == 1 else _LIST_PAGED.format(n=page_n)

            html = _curl(list_url)
            if not html:
                print(f"[{_SITE_ID}] page {page_n}: failed to fetch list, stopping.")
                break

            # ── Extract communiqués items ────────────────────────────────
            items = _extract_communiques(html)
            if not items:
                empty_streak += 1
                if empty_streak >= 2:
                    print(
                        f"[{_SITE_ID}] no communiqués on page {page_n} (streak {empty_streak}), stopping."
                    )
                    break
                page_n += 1
                continue

            # ── Process each item ────────────────────────────────────────
            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    detail = _fetch_detail(item_url)
                    if not detail:
                        print(f"[{_SITE_ID}] item {item_url} failed: could not fetch detail")
                        continue

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] item {item_url} skipped: abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    slug = item_url.rstrip("/").rsplit("/", 1)[-1]
                    pdf_url = detail.get("pdf_url")
                    original_filename = pdf_url.split("/")[-1] if pdf_url else None

                    metadata = {
                        "posted_date": item.get("date_raw"),
                        "list_abstract": item.get("list_abstract", ""),
                        "category_raw": "Communiqués",
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": detail.get("title") or item.get("title") or slug,
                        "abstract": abstract,
                        "published_date": detail.get("published_date") or item.get("published_date"),
                        "listed_date": item.get("published_date"),
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": _PUBLISHER,
                        "category": "Communiqués",
                        "keywords": None,
                        "authors": None,
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}: {paper['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item_url} failed: {exc}")
                    continue

                time.sleep(_RATE)

            # ── Pagination housekeeping ──────────────────────────────────
            if new_on_page == 0:
                empty_streak += 1
                if empty_streak >= 2:
                    print(
                        f"[{_SITE_ID}] no new items on page {page_n} (streak {empty_streak}), stopping."
                    )
                    break
            else:
                empty_streak = 0

            page_n += 1

        return saved
