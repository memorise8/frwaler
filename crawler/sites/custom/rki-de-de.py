# -*- coding: utf-8 -*-
"""Crawler for RKI (Robert Koch-Institut) Meldungen und Pressemitteilungen."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

# Absolute import — spec_from_file_location has no package context
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _BS4_AVAILABLE = True
except ImportError:
    _BS4_AVAILABLE = False

_SITE_ID = "rki-de-de"
_BASE_URL = "https://www.rki.de"
_LIST_URL = (
    "https://www.rki.de/DE/Aktuelles/Neuigkeiten-und-Presse/Meldungen-PM/"
    "meldungen-pressemitteilungen-node.html"
)
# URL-encoded page parameter used by Government Site Builder
_PAGE_PARAM_TPL = "gtp=16956152_Dokumente%253D{page}"
_ITEMS_PER_PAGE = 25
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 50
_WALL_CLOCK_LIMIT = 25 * 60  # 25 minutes in seconds


def _make_soup(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags, unescape entities, normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS compatibility and retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            body = result.stdout.decode("utf-8", errors="replace").strip()
            if body:
                return body
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt+1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = 1 * (3 ** attempt)  # 1s, 3s, 9s
            print(f"[{_SITE_ID}] Retrying in {wait}s...")
            time.sleep(wait)
    return None


def _parse_date_de(raw: str) -> str:
    """Convert German date '05.05.2026' → '2026-05-05'."""
    raw = raw.strip()
    m = re.match(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return raw


def _extract_post_number(url: str) -> str | None:
    """Extract YYYYMMDD from URL filename like '2026-05-05_Pflegebericht.html'."""
    filename = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    filename = re.sub(r"\.html?$", "", filename)
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", filename)
    if m:
        return f"{m.group(1)}{m.group(2)}{m.group(3)}"
    return None


def _page_url(page: int) -> str:
    if page <= 1:
        return _LIST_URL
    return f"{_LIST_URL}?{_PAGE_PARAM_TPL.format(page=page)}"


def _parse_list_page(html: str) -> list[dict]:
    """Parse a listing page, return list of teaser dicts."""
    items = []
    if not _BS4_AVAILABLE or not html:
        return items

    soup = _make_soup(html)
    if not soup:
        return items

    for teaser in soup.find_all("div", class_="c-teaser-search-result"):
        try:
            # URL
            link_tag = teaser.find("a", class_="c-teaser-search-result__main-link")
            if not link_tag or not link_tag.get("href"):
                continue
            url = urljoin(_BASE_URL, link_tag["href"].split("?")[0])

            # Title
            h3 = teaser.find("h3", class_="c-teaser-search-result__headline")
            title = _strip_tags(str(h3)) if h3 else ""

            # Type + date from topline
            category = ""
            listed_date = ""
            topline = teaser.find("p", class_="c-topline")
            if topline:
                spans = topline.find_all("span", class_="c-topline__element")
                for span in spans:
                    cls = span.get("class", [])
                    if "is-type" in cls:
                        category = span.get_text(strip=True)
                    elif "is-date" in cls:
                        listed_date = _parse_date_de(span.get_text(strip=True))

            # Teaser text
            p_text = teaser.find("p", class_="c-teaser-search-result__text")
            teaser_text = _strip_tags(str(p_text)) if p_text else ""

            items.append({
                "url": url,
                "title": title,
                "category": category,
                "listed_date": listed_date,
                "teaser_text": teaser_text,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] list item parse error: {exc}")
            continue

    return items


def _parse_detail_page(html: str) -> dict:
    """Parse a detail page, return dict with abstract, published_date, pdf_url."""
    result = {"abstract": "", "published_date": "", "pdf_url": None, "original_filename": None}
    if not _BS4_AVAILABLE or not html:
        return result

    soup = _make_soup(html)
    if not soup:
        return result

    # Date from subline
    subline = soup.find("p", class_="l-article-wrapper__subline")
    if subline:
        raw_date = subline.get_text(strip=True)
        raw_date = re.sub(r"Stand\s*:?\s*", "", raw_date).strip()
        result["published_date"] = _parse_date_de(raw_date)

    # Full article body
    content_div = soup.find("div", class_="l-article-wrapper__content")
    if content_div:
        # Remove navigation / footer elements
        for tag in content_div.find_all(["nav", "footer", "script", "style"]):
            tag.decompose()
        text = content_div.get_text(separator=" ", strip=True)
        text = re.sub(r"\s+", " ", text).strip()
        result["abstract"] = text

    # PDF links
    pdf_url = None
    original_filename = None
    for a in (soup.find_all("a", href=True) if soup else []):
        href = a["href"]
        if re.search(r"\.pdf(\?|$)", href, re.I):
            pdf_url = urljoin(_BASE_URL, href)
            fname = urlparse(href).path.rstrip("/").rsplit("/", 1)[-1]
            original_filename = fname if fname else None
            break
    result["pdf_url"] = pdf_url
    result["original_filename"] = original_filename

    return result


class RkiDeDeCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: rki-de-de"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay: float = 1.0):
        super().__init__(db_conn, delay=delay)

    def crawl(self, limit=None) -> int:
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_label = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock safety
            if time.time() - start_time > _WALL_CLOCK_LIMIT:
                print(f"[{_SITE_ID}] Wall-clock limit reached ({_WALL_CLOCK_LIMIT}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            url = _page_url(page)
            print(f"[{_SITE_ID}] Fetching list page {page}: {url}")
            html = _curl_get(url)
            if not html:
                print(f"[{_SITE_ID}] Failed to fetch page {page}. Stopping.")
                break

            items = _parse_list_page(html)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page}. Done.")
                break

            # Deduplicate across pages
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All items on page {page} already seen. Done.")
                break
            for it in new_items:
                seen_urls.add(it["url"])

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]

                # Extract slug for external_id and post_number
                path_slug = urlparse(item_url).path.rstrip("/").rsplit("/", 1)[-1]
                path_slug = re.sub(r"\.html?$", "", path_slug)
                post_number = _extract_post_number(item_url)

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] Detail fetch failed: {item_url}. Skipping.")
                        continue

                    detail = _parse_detail_page(detail_html)

                    # Build abstract: prefer detail body, fall back to teaser
                    abstract = detail.get("abstract", "").strip()
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        abstract = item.get("teaser_text", "").strip()
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars) for {item_url}. Skipping.")
                        continue

                    published_date = detail.get("published_date") or item.get("listed_date", "")
                    listed_date = item.get("listed_date", "")

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": path_slug,
                        "post_number": post_number,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "authors": None,
                        "publisher": "Robert Koch-Institut",
                        "department": None,
                        "journal": None,
                        "doi": None,
                        "keywords": None,
                        "category": item.get("category", ""),
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "item_type": item.get("category", ""),
                            "slug": path_slug,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_label}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    print(f"[{_SITE_ID}] Interrupted. Saved {saved} so far.")
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Item failed ({item_url}): {exc}. Continuing.")
                    continue

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            # Check if there's a next page
            has_next = _has_next_page(html, page)
            if not has_next:
                print(f"[{_SITE_ID}] No more pages after page {page}. Done.")
                break

            page += 1

        print(f"[{_SITE_ID}] Crawl complete. Total saved: {saved}")
        return saved


def _has_next_page(html: str, current_page: int) -> bool:
    """Detect whether a next-page link exists for pages after current_page."""
    next_page = current_page + 1
    pattern = rf"Dokumente(?:%253D|%3D|=){next_page}"
    return bool(re.search(pattern, html))
