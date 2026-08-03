# -*- coding: utf-8 -*-
"""Crawler for Forsvarsministeriet (Danish Ministry of Defence) news.

Starting URL: https://www.fmn.dk/da/nyheder/
List API:     GET /ListPage/UpdateList?sorting=PublishedDescending&rootId=791
              &pagetype=49&count=500&pageAuthority=269&cultureInfo=da
              &intervals=<PERIOD>,
              Returns HTML fragment with <li class="item col-12"> entries.
              Intervals: 2024-2026 | 2019-2023 | 2014-2018 | 2009-2013 | 2004-2008
Detail pages: /da/nyheder/YEAR/slug/ — div.news-title (lead) + div.editor-content (body).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urljoin, urlparse

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.fmn.dk"
_LIST_API = _BASE + "/ListPage/UpdateList"
_START_URL = _BASE + "/da/nyheder/"

# All available intervals (newest first — walk these as logical "pages")
_INTERVALS = [
    "2024-2026",
    "2019-2023",
    "2014-2018",
    "2009-2013",
    "2004-2008",
]

_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))        # safety cap across all intervals
_WALL_MINUTES = 25
_MIN_ABSTRACT = 50      # skip items whose abstract is shorter than this

# Danish month names → month number
_DA_MONTHS = {
    "januar": 1, "februar": 2, "marts": 3, "april": 4,
    "maj": 5, "juni": 6, "juli": 7, "august": 8,
    "september": 9, "oktober": 10, "november": 11, "december": 12,
}


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _curl(url: str, retries: int = 3, extra_headers: list | None = None) -> str:
    """Fetch url via curl (TLS-1.3, insecure). Returns text or '' on failure."""
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
                "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.8",
            ]
            if extra_headers:
                for h in extra_headers:
                    cmd += ["-H", h]
            cmd.append(url)
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            print(f"[fmn-dk-da] curl exit {result.returncode} for {url}")
        except subprocess.TimeoutExpired:
            print(f"[fmn-dk-da] curl timeout (attempt {attempt + 1}/{retries}) for {url}")
        except Exception as exc:
            print(f"[fmn-dk-da] curl error (attempt {attempt + 1}/{retries}): {exc}")
    return ""


# ---------------------------------------------------------------------------
# HTML parser
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


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str | None:
    """Parse Danish date like '24. februar, 2026 - Kl. 15.00' → '2026-02-24'."""
    if not raw:
        return None
    m = re.search(r"(\d{1,2})\.\s+(\w+),?\s+(\d{4})", raw)
    if not m:
        return None
    day, month_da, year = m.group(1), m.group(2).lower(), m.group(3)
    month_num = _DA_MONTHS.get(month_da)
    if not month_num:
        return None
    return f"{year}-{month_num:02d}-{int(day):02d}"


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _parse_list_html(html: str) -> list[dict]:
    """Return list of {url, title, date_raw, description, categories} from list API HTML."""
    soup = _make_soup(html)
    if not soup:
        return []
    items = []
    for li in soup.select("li.item"):
        try:
            a = li.find("a")
            if not a:
                continue
            href = a.get("href", "")
            if not href or "/nyheder/" not in href:
                continue

            title_el = li.find("h2", class_="title")
            title = title_el.get_text(strip=True) if title_el else ""

            date_el = li.find("span", class_="date")
            date_raw = date_el.get_text(strip=True) if date_el else ""

            desc_el = li.find("div", class_="description")
            description = desc_el.get_text(strip=True) if desc_el else ""

            cats = [
                c.get_text(strip=True)
                for c in li.find_all("span", class_="category")
                if c.get_text(strip=True) not in ("", "/")
            ]

            items.append({
                "url": href,
                "title": title,
                "date_raw": date_raw,
                "description": description,
                "categories": cats,
            })
        except Exception:
            continue
    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail(url: str) -> dict | None:
    """Fetch detail page; return {abstract, published_date, pdf_url, title}."""
    html = _curl(url)
    if not html:
        return None

    soup = _make_soup(html)
    if not soup:
        # Raw regex fallback
        body = re.sub(r"<[^>]+>", " ", html)
        body = re.sub(r"\s+", " ", body).strip()
        return {"abstract": body[:2000], "published_date": None, "pdf_url": None}

    parts: list[str] = []

    # Lead paragraph (manchet)
    news_title = soup.find(class_="news-title")
    if news_title:
        lead = news_title.get_text(separator=" ", strip=True)
        if lead:
            parts.append(lead)

    # Main body
    editor = soup.find(id="mt-3") or soup.find(class_="editor-content")
    if editor:
        # Remove sidebar / factbox / social media noise
        for noise in editor.find_all(class_=["factbox", "social-media", "seperator"]):
            noise.decompose()
        body_text = editor.get_text(separator=" ", strip=True)
        body_text = re.sub(r"\s+", " ", body_text).strip()
        if body_text:
            parts.append(body_text)

    abstract = " ".join(parts)
    abstract = re.sub(r"\s+", " ", abstract).strip()

    # Date
    date_el = soup.find(class_="news-date")
    date_raw = date_el.get_text(strip=True) if date_el else ""
    published_date = _parse_date(date_raw)

    # PDF links
    pdf_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".pdf"):
            pdf_url = href if href.startswith("http") else _BASE + href
            break

    return {
        "abstract": abstract,
        "published_date": published_date,
        "pdf_url": pdf_url,
    }


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class FmnDkDaCrawler(BaseCrawler):
    site_id = "fmn-dk-da"
    site_name = "Custom: fmn-dk-da"
    base_url = "https://www.fmn.dk"

    def _fetch_interval(self, interval: str | None) -> str:
        """Call the list API for a given interval (or no interval = all items)."""
        params = (
            f"sorting=PublishedDescending"
            f"&rootId=791"
            f"&pagetype=49"
            f"&count=500"
            f"&pageAuthority=269"
            f"&cultureInfo=da"
        )
        if interval:
            params += f"&intervals={interval},"
        url = f"{_LIST_API}?{params}"
        return _curl(url)

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        limit_disp = limit if limit is not None else "∞"
        start_time = time.time()
        page_count = 0

        for interval in _INTERVALS:
            if limit is not None and saved >= limit:
                break
            if page_count >= _MAX_PAGES:
                print(f"[fmn-dk-da] safety cap of {_MAX_PAGES} interval-pages reached; stopping")
                break
            elapsed = (time.time() - start_time) / 60
            if elapsed >= _WALL_MINUTES:
                print(f"[fmn-dk-da] wall-clock budget {_WALL_MINUTES}m reached; stopping")
                break

            page_count += 1
            print(f"[fmn-dk-da] interval {interval}: saved {saved}/{limit_disp}")
            html = self._fetch_interval(interval)
            if not html:
                print(f"[fmn-dk-da] empty response for interval {interval}, skipping")
                continue

            list_items = _parse_list_html(html)
            if not list_items:
                print(f"[fmn-dk-da] no items in interval {interval}")
                continue

            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                raw_url = item["url"]
                full_url = raw_url if raw_url.startswith("http") else _BASE + raw_url
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)

                # external_id / post_number: strip /da/nyheder/ prefix and trailing /
                slug = raw_url.removeprefix("/da/nyheder/").strip("/")
                listed_date = _parse_date(item["date_raw"])

                # Fetch detail page
                try:
                    detail = _parse_detail(full_url)
                except Exception as exc:
                    print(f"[fmn-dk-da] detail fetch failed for {full_url}: {exc}")
                    detail = None

                if detail is None:
                    detail = {}

                abstract = detail.get("abstract") or item.get("description", "")
                if not abstract or len(abstract) < _MIN_ABSTRACT:
                    print(f"[fmn-dk-da] abstract too short ({len(abstract or '')} chars), skipping: {full_url}")
                    time.sleep(0.5)
                    continue

                published_date = detail.get("published_date") or listed_date

                # Build categories string
                cats = item.get("categories", [])
                publisher = cats[0] if cats else "Forsvarsministeriet"
                category = cats[1] if len(cats) > 1 else None

                metadata = {
                    "posted_date": item.get("date_raw"),
                    "categories": cats,
                    "interval": interval,
                }

                paper = {
                    "site_id": self.site_id,
                    "external_id": slug,
                    "post_number": slug,
                    "title": item["title"] or "(untitled)",
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": listed_date,
                    "url": full_url,
                    "pdf_url": detail.get("pdf_url"),
                    "publisher": publisher,
                    "category": category,
                    "authors": None,
                    "keywords": None,
                    "doi": None,
                    "original_filename": None,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                }

                try:
                    self._save_paper(paper)
                    saved += 1
                except Exception as exc:
                    print(f"[fmn-dk-da] save failed for {full_url}: {exc}")
                    continue

                time.sleep(self._delay)

            # Progress every "page" (interval)
            if page_count % 1 == 0:
                print(f"[fmn-dk-da] page {page_count}: saved {saved}/{limit_disp}")

        print(f"[fmn-dk-da] done — saved {saved} items")
        return saved
