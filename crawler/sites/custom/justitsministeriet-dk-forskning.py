# -*- coding: utf-8 -*-
"""Crawler for Justitsministeriets Forskningsrapporter (Danish MoJ research reports).

Starting URL: https://www.justitsministeriet.dk/forskning/forskningsrapporter/

Structure: WordPress site with 6 year-range sub-pages.
- Newer pages (2017+): <h3><a href="PDF">Title</a></h3> followed by <p>(date)<br>Abstract</p>
- Older pages (<2017): <p><a href="PDF">Title</a> (date)</p> — no abstracts

No individual detail pages; all data lives on the year-range listing pages.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from html import unescape

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "justitsministeriet-dk-forskning"
_BASE_URL = "https://www.justitsministeriet.dk"
_START_URL = _BASE_URL + "/forskning/forskningsrapporter/"
_MAX_PAGES = 200
_WALL_MINUTES = 25
_MIN_ABSTRACT = 50

# Fallback ordered list (newest first) if discovery fails
_YEAR_RANGE_FALLBACK = [
    _BASE_URL + "/forskning/forskningsrapporter/forskningsrapporter-2023-2026/",
    _BASE_URL + "/forskning/forskningsrapporter/forskningsrapporter-2020-2022/",
    _BASE_URL + "/forskning/forskningsrapporter/forskningsrapporter-2017-2019/",
    _BASE_URL + "/forskning/forskningsrapporter/forskningsrapporter-2011-2016/",
    _BASE_URL + "/forskning/forskningsrapporter/forskningsrapporter-2005-2010/",
    _BASE_URL + "/forskning/forskningsrapporter/forskningsrapporter-1999-2004/",
]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl(url: str, retries: int = 3) -> str:
    """Fetch URL with curl (TLS-1.3, insecure). Returns decoded text or '' on failure."""
    backoff = [1, 3, 9]
    for attempt in range(retries):
        if attempt > 0:
            time.sleep(backoff[attempt - 1])
        try:
            cmd = [
                "curl", "--tls-max", "1.3", "-skL",
                "--max-time", "30",
                "--user-agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "-H", "Accept: text/html,*/*",
                "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.8",
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[{_SITE_ID}] curl exit {result.returncode} for {url}")
        except subprocess.TimeoutExpired:
            print(f"[{_SITE_ID}] curl timeout (attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}): {exc}")
    return ""


# ---------------------------------------------------------------------------
# HTML / parse helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
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


def _parse_date(date_str: str) -> str | None:
    """Convert DD-MM-YYYY (or DD-MM-YY) → ISO YYYY-MM-DD."""
    if not date_str:
        return None
    m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{2,4})", date_str)
    if m:
        day, month, year = m.group(1), m.group(2), m.group(3)
        if len(year) == 2:
            year = "20" + year
        try:
            return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
        except ValueError:
            pass
    m2 = re.search(r"(\d{4}-\d{2}-\d{2})", date_str)
    if m2:
        return m2.group(1)
    return None


def _pdf_filename(url: str) -> str | None:
    """Extract bare filename from a URL."""
    if not url:
        return None
    tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
    if "." in tail and len(tail) <= 200:
        return tail
    return None


def _external_id(url: str) -> str:
    """Derive a dedup-safe external_id from a URL (filename without extension)."""
    fname = _pdf_filename(url) or url
    if "." in fname:
        fname = fname.rsplit(".", 1)[0]
    return fname[:200]


def _parse_page(soup, page_url: str) -> list[dict]:
    """Return a list of raw record dicts parsed from one year-range page."""
    records: list[dict] = []
    try:
        content_div = soup.find("div", class_="entry-content")
        if not content_div:
            return records

        # Newer format: h3 with link → following p with (date)\nabstract
        h3_tags = [h for h in content_div.find_all("h3") if h.find("a")]
        if h3_tags:
            for h3 in h3_tags:
                link = h3.find("a")
                if not link:
                    continue
                href = (link.get("href") or "").strip()
                if not href:
                    continue
                if href.startswith("/"):
                    href = _BASE_URL + href

                title = unescape(link.get_text(strip=True))
                if not title:
                    continue

                abstract = ""
                date_str = ""
                next_p = h3.find_next_sibling("p")
                if next_p:
                    # Use newline as separator to split date from abstract
                    p_text = next_p.get_text(separator="\n", strip=True)
                    # Date line: (DD-MM-YYYY) at the start
                    dm = re.match(r"^\((\d{1,2}[-./]\d{1,2}[-./]\d{2,4})\)\s*\n?", p_text)
                    if dm:
                        date_str = dm.group(1)
                        abstract = p_text[dm.end():].strip()
                    else:
                        # Try finding date anywhere in first line
                        first_line = p_text.split("\n")[0]
                        dm2 = re.search(r"\((\d{1,2}[-./]\d{1,2}[-./]\d{2,4})\)", first_line)
                        if dm2:
                            date_str = dm2.group(1)
                        abstract = re.sub(r"^\([^)]+\)\s*\n?", "", p_text, count=1).strip()

                records.append({
                    "title": title,
                    "href": href,
                    "date_str": date_str,
                    "abstract": abstract,
                    "page_url": page_url,
                })
            return records

        # Older format: <p><a href="PDF">Title</a> (date)</p>
        for p in content_div.find_all("p"):
            link = p.find("a")
            if not link:
                continue
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if href.startswith("/"):
                href = _BASE_URL + href

            title = unescape(link.get_text(strip=True))
            if not title:
                continue

            p_text = p.get_text(strip=True)
            dm = re.search(r"\((\d{1,2}[-./]\d{1,2}[-./]\d{2,4})\)", p_text)
            date_str = dm.group(1) if dm else ""

            records.append({
                "title": title,
                "href": href,
                "date_str": date_str,
                "abstract": "",
                "page_url": page_url,
            })

    except Exception as exc:
        print(f"[{_SITE_ID}] page parse error for {page_url}: {exc}")

    return records


def _discover_year_pages(html: str) -> list[str]:
    """Extract year-range sub-page URLs from the main listing page HTML."""
    pattern = re.compile(
        r'href="(https://www\.justitsministeriet\.dk'
        r'/forskning/forskningsrapporter/forskningsrapporter-\d{4}-\d{4}/)"'
    )
    seen: set = set()
    result = []
    for m in pattern.finditer(html):
        url = m.group(1)
        if url not in seen:
            seen.add(url)
            result.append(url)
    # Sort descending so newest pages come first
    result.sort(reverse=True)
    return result


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class JustitsministerietForsknCrawler(BaseCrawler):
    site_id = "justitsministeriet-dk-forskning"
    site_name = "Custom: justitsministeriet-dk-forskning"
    base_url = "https://www.justitsministeriet.dk"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_display = limit if limit is not None else "inf"
        deadline = time.time() + _WALL_MINUTES * 60

        # Discover sub-pages; fall back to hard-coded list
        main_html = _curl(_START_URL)
        year_pages = _discover_year_pages(main_html) if main_html else []
        if not year_pages:
            print(f"[{_SITE_ID}] discovery failed, using fallback page list.")
            year_pages = _YEAR_RANGE_FALLBACK

        page_count = 0
        for page_url in year_pages:
            if time.time() > deadline:
                print(
                    f"[{_SITE_ID}] {_WALL_MINUTES}-minute wall-clock budget reached, "
                    "stopping cleanly."
                )
                break
            if limit is not None and saved >= limit:
                break
            if page_count >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")
                break

            page_count += 1
            if page_count == 1 or page_count % 10 == 0:
                print(f"[{_SITE_ID}] page {page_count}: saved {saved}/{limit_display}")

            html = _curl(page_url)
            if not html:
                print(f"[{_SITE_ID}] page {page_count}: failed to fetch {page_url}, skipping.")
                continue

            try:
                soup = _make_soup(html)
            except Exception as exc:
                print(f"[{_SITE_ID}] page {page_count}: soup error for {page_url}: {exc}")
                soup = None

            if not soup:
                print(f"[{_SITE_ID}] page {page_count}: could not parse {page_url}, skipping.")
                continue

            records = _parse_page(soup, page_url)
            if not records:
                print(f"[{_SITE_ID}] page {page_count}: no records found on {page_url}")
                continue

            print(
                f"[{_SITE_ID}] page {page_count} ({page_url.split('/')[-2]}): "
                f"{len(records)} records found"
            )

            for rec in records:
                if limit is not None and saved >= limit:
                    break
                if time.time() > deadline:
                    break

                try:
                    href = rec["href"]
                    title = rec["title"]
                    abstract = rec["abstract"]
                    date_str = rec["date_str"]

                    if not title:
                        continue

                    # URL deduplication
                    dedup_key = href or title
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    # Skip items with no / very short abstract
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{_SITE_ID}] skip '{title[:50]}': "
                            f"abstract {len(abstract)} chars < {_MIN_ABSTRACT}"
                        )
                        continue

                    pub_date = _parse_date(date_str)
                    ext_id = _external_id(href)
                    orig_filename = _pdf_filename(href)
                    pdf_url = href if (href.lower().endswith(".pdf")) else None
                    # For non-PDF links treat as meta_url only
                    meta_url = rec["page_url"]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "url": meta_url,
                        "pdf_url": pdf_url or "",
                        "authors": json.dumps([]),
                        "keywords": json.dumps([]),
                        "department": "Justitsministeriets Forskningsenhed",
                        "publisher": "Justitsministeriets Forskningsenhed",
                        "category": "Forskningsrapport",
                        "original_filename": orig_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_str,
                                "originalFilename": orig_filename,
                                "source_page": rec["page_url"],
                                "direct_url": href,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved [{saved}] {title[:60]}")

                    time.sleep(0.2)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item '{rec.get('title', '?')[:50]}' failed: {exc}")
                    continue

            time.sleep(1.0)

        print(f"[{_SITE_ID}] crawl complete: {saved} records saved.")
        return saved
