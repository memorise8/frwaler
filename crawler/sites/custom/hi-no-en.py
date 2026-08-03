# -*- coding: utf-8 -*-
"""Crawler for Institute of Marine Research (hi.no) nettrapporter (English reports)."""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.hi.no"
_LIST_URL = "https://www.hi.no/en/hi/nettrapporter"
_PAGE_SIZE = 30
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl (TLS max 1.3) with exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {_USER_AGENT}",
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw and len(raw) > 200:
                return raw.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                print(f"[hi-no-en] Empty response for {url}, retry {attempt+1}...")
                time.sleep(waits[attempt])
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(waits[attempt])
        except Exception as exc:
            print(f"[hi-no-en] curl attempt {attempt+1} error: {exc}")
            if attempt < retries - 1:
                time.sleep(waits[attempt])
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML — fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    """Convert DD.MM.YYYY → YYYY-MM-DD; return as-is for other formats."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw


# ---------------------------------------------------------------------------
# List page parser
# ---------------------------------------------------------------------------

def _parse_list_items(html: str) -> list:
    """Return list of (href, title, listed_date_raw) from a list page."""
    soup = _make_soup(html)
    if not soup:
        return []
    results = []
    for div in soup.find_all("div", class_="item"):
        if "reportitem" not in (div.get("class") or []):
            continue
        h3 = div.find("h3", class_="list-item-header")
        if not h3:
            continue
        a = h3.find("a")
        if not a or not a.get("href"):
            continue
        href = a["href"].strip()
        title = a.get_text(strip=True)
        if not href or not title:
            continue
        date_tag = div.find("div", class_="date")
        listed_date_raw = date_tag.get_text(strip=True) if date_tag else None
        results.append((href, title, listed_date_raw))
    return results


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

def _parse_detail(html: str, href: str) -> dict | None:
    """Parse a report detail page and return a paper dict, or None on failure."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[hi-no-en] soup error for {href}: {exc}")
        return None
    if not soup:
        return None

    # post_number / external_id — prefer numeric data-sqlid
    page_div = soup.find(attrs={"data-sqlid": True})
    sqlid = page_div["data-sqlid"].strip() if page_div else None
    slug = href.rstrip("/").rsplit("/", 1)[-1]
    external_id = sqlid if sqlid else slug

    # Title
    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else None
    if not title:
        return None

    # Authors from span.person (exclude the outer external_authors span)
    authors_list = []
    for span in soup.find_all("span", class_="person"):
        raw = span.get_text(separator=" ", strip=True)
        name = re.sub(r"\s*\([^)]*\)", "", raw).strip()          # drop "(IMR)" etc.
        name = re.sub(r"\s+(and|og)\s*$", "", name, flags=re.IGNORECASE).strip()
        name = name.rstrip(",").strip()
        if name:
            authors_list.append(name)
    authors = "; ".join(authors_list) if authors_list else None

    # articleinfo block for metadata extraction via regex
    articleinfo = soup.find("div", class_="articleinfo")
    ai_str = str(articleinfo) if articleinfo else ""

    published_date = None
    pub_m = re.search(r"Published:.*?<em>(.*?)</em>", ai_str, re.DOTALL)
    if pub_m:
        published_date = _parse_date(pub_m.group(1).strip())

    issn = None
    issn_m = re.search(r"ISSN:.*?<em>(.*?)</em>", ai_str)
    if issn_m:
        issn = issn_m.group(1).strip()

    # Report series, e.g. "Rapport fra havforskningen 2026-24"
    report_series = None
    if articleinfo:
        for em in articleinfo.find_all("em"):
            text = em.get_text(strip=True)
            if re.search(r"\d{4}-\d+", text):
                report_series = text
                break

    # Program name
    program = None
    prog_m = re.search(r"Program:.*?<em>(.*?)</em>", ai_str, re.DOTALL)
    if prog_m:
        program = re.sub(r"\s+", " ", prog_m.group(1).strip())

    # English abstract — first <h2> containing "Summary" → next .content div
    abstract = None
    for h2 in soup.find_all("h2"):
        if "Summary" in h2.get_text():
            content = h2.find_next("div", class_="content")
            if content:
                abstract = content.get_text(separator=" ", strip=True)
            break

    # Fallback: any intro / ingress block
    if not abstract:
        for cls_name in ("intro", "ingress", "article-intro", "lead"):
            tag = soup.find(class_=cls_name)
            if tag:
                text = tag.get_text(separator=" ", strip=True)
                if len(text) >= 50:
                    abstract = text
                    break

    # PDF URL
    pdf_url = None
    pdf_a = soup.find("a", href=re.compile(r"report-pdf\?id="))
    if pdf_a:
        ph = pdf_a["href"]
        pdf_url = (_BASE + ph) if ph.startswith("/") else ph

    detail_url = (_BASE + href) if href.startswith("/") else href

    metadata: dict = {"slug": slug}
    if sqlid:
        metadata["sqlid"] = sqlid
    if report_series:
        metadata["report_series"] = report_series
    if issn:
        metadata["issn"] = issn
    if program:
        metadata["program"] = program

    return {
        "external_id": external_id,
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "authors": authors,
        "publisher": "Institute of Marine Research (IMR)",
        "journal": report_series,
        "url": detail_url,
        "pdf_url": pdf_url,
        "category": "Marine Research Report",
        "metadata": metadata,
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class HiNoEnCrawler(BaseCrawler):
    """Crawler for hi.no nettrapporter (English-language reports)."""

    site_id = "hi-no-en"
    site_name = "Custom: hi-no-en"
    base_url = "https://www.hi.no"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page_idx in range(_MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > _MAX_SECONDS:
                print(f"[hi-no-en] 25-minute budget reached on page {page_idx+1}, stopping cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            start = page_idx * _PAGE_SIZE
            list_url = (
                f"{_LIST_URL}?start={start}&query="
                if start > 0
                else _LIST_URL
            )

            try:
                list_html = _curl_get(list_url)
            except KeyboardInterrupt:
                raise

            if not list_html:
                print(f"[hi-no-en] Failed to fetch list page {page_idx+1}, stopping.")
                break

            items = _parse_list_items(list_html)
            if not items:
                print(f"[hi-no-en] No items on page {page_idx+1}, stopping.")
                break

            if page_idx % 10 == 0:
                print(f"[hi-no-en] page {page_idx+1}: saved {saved}/{limit_str}")

            new_on_page = 0
            for href, title, listed_date_raw in items:
                if limit is not None and saved >= limit:
                    break

                full_url = (_BASE + href) if href.startswith("/") else href
                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                new_on_page += 1

                time.sleep(self._delay)

                try:
                    detail_html = _curl_get(full_url)
                    if not detail_html:
                        print(f"[hi-no-en] item {href} fetch failed, skipping.")
                        continue

                    paper = _parse_detail(detail_html, href)
                    if not paper:
                        print(f"[hi-no-en] item {href} parse failed, skipping.")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 100:
                        print(
                            f"[hi-no-en] item {href} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    paper["posted_date"] = _parse_date(listed_date_raw)
                    if not paper.get("published_date"):
                        paper["published_date"] = paper["posted_date"]

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[hi-no-en] item {href} failed: {exc}")
                    continue

            # All items on page already seen → dedup loop detected
            if new_on_page == 0:
                print(f"[hi-no-en] All items on page {page_idx+1} already seen, stopping.")
                break

            # End-of-pagination detection
            if len(items) < _PAGE_SIZE:
                print(f"[hi-no-en] Last page reached ({len(items)} items), done.")
                break
            if 'disabled" aria-disabled="true">Next' in list_html:
                print(f"[hi-no-en] No next page link found, done.")
                break

        print(f"[hi-no-en] Done. Total saved: {saved}")
        return saved
