# -*- coding: utf-8 -*-
"""PNNL (Pacific Northwest National Laboratory) news crawler.

Starting URL : https://www.pnnl.gov/news?news%5B0%5D=type%3A23
List API     : Drupal 9 Views AJAX POST at /views/ajax
               view_name=news_listing, display=default, news[]=type:23
               Returns JSON with 'insert' command containing HTML cards (~20/page).
Pagination   : page=0, 1, 2, ... (0-indexed)
Detail       : GET /news-media/<slug> — full article body in .l-story__body
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.pnnl.gov"
_VIEWS_AJAX_URL = "https://www.pnnl.gov/views/ajax"
_VIEW_DOM_ID = "736740584bf3f9849f61385e81a0e5ffb9fe49738b5a84d3d5977559b1d79a5c"
_ITEMS_PER_PAGE = 20
_SAFETY_CAP = 200  # max pages before hard stop
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HTTP helpers (curl-based; browser headers bypass the WAF 403)
# ---------------------------------------------------------------------------

def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """GET via curl with browser headers. Returns text or None after retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "--compressed",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Connection: keep-alive",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[pnnl-gov-news] GET {url} empty (attempt {attempt+1}/{retries}), retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[pnnl-gov-news] GET {url} error: {exc} (attempt {attempt+1}/{retries}), retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[pnnl-gov-news] GET {url} failed after {retries} attempts: {exc}")
    return None


def _curl_post_list(page: int = 0, timeout: int = 30, retries: int = 3) -> str | None:
    """POST to Drupal Views AJAX for news listing page N. Returns JSON text or None."""
    post_data = (
        f"view_name=news_listing"
        f"&view_display_id=default"
        f"&view_args="
        f"&view_path=%2Fnode%2F27"
        f"&view_base_path="
        f"&view_dom_id={_VIEW_DOM_ID}"
        f"&pager_element=0"
        f"&news%5B%5D=type%3A23"
        f"&_drupal_ajax=1"
        f"&page={page}"
    )
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "--compressed",
        "-X", "POST",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: application/json, text/javascript, */*; q=0.01",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        "-H", "X-Requested-With: XMLHttpRequest",
        "-H", "Referer: https://www.pnnl.gov/news",
        "--data", post_data,
        _VIEWS_AJAX_URL,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout.strip():
                return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[pnnl-gov-news] AJAX page {page} empty (attempt {attempt+1}/{retries}), retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[pnnl-gov-news] AJAX page {page} error: {exc} (attempt {attempt+1}/{retries}), retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[pnnl-gov-news] AJAX page {page} failed after {retries} attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
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


def _parse_date(text: str) -> str:
    """Convert 'MAY 8, 2026' or 'May 8, 2026' → '2026-05-08'. Empty string on failure."""
    if not text:
        return ""
    text = text.strip()
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    # Regex fallback for mixed-case or slightly different formats
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),?\s+(\d{4})", text)
    if m:
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
    return ""


# ---------------------------------------------------------------------------
# List-page card parser
# ---------------------------------------------------------------------------

def _parse_list_response(raw: str) -> list[dict]:
    """Parse Drupal Views AJAX JSON → list of card dicts.

    Each dict has keys: url, title, date_str, news_type, summary.
    """
    try:
        commands = json.loads(raw)
    except json.JSONDecodeError:
        idx = raw.find("[{")
        if idx < 0:
            return []
        try:
            commands = json.loads(raw[idx:])
        except Exception:
            return []

    insert_html = ""
    for cmd in commands:
        if isinstance(cmd, dict) and cmd.get("command") == "insert":
            insert_html = cmd.get("data", "")
            break

    if not insert_html:
        return []

    try:
        soup = _make_soup(insert_html)
    except Exception:
        return []
    if not soup:
        return []

    cards = []
    for card in soup.find_all("article", class_=re.compile(r"\bteaser\b")):
        try:
            title_el = card.select_one(".teaser__title a")
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            if not href:
                continue
            url = href if href.startswith("http") else _BASE_URL + href

            # Date and content-type from .meta__item elements
            meta_items = card.select(".meta__item")
            date_str = meta_items[0].get_text(strip=True) if len(meta_items) > 0 else ""
            news_type = meta_items[1].get_text(strip=True) if len(meta_items) > 1 else ""

            # Short teaser summary
            summary_el = card.select_one(".teaser__summary")
            summary = ""
            if summary_el:
                for rm in summary_el.select(".teaser__readmore, .more-link"):
                    rm.decompose()
                summary = summary_el.get_text(separator=" ", strip=True)

            cards.append({
                "url": url,
                "title": title,
                "date_str": date_str,
                "news_type": news_type,
                "summary": summary,
            })
        except Exception as exc:
            print(f"[pnnl-gov-news] card parse error: {exc}")
            continue

    return cards


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail(html: str) -> dict:
    """Parse a detail page. Returns dict with title, date_str, news_type,
    author, node_id, abstract, subtitle."""
    result: dict = {
        "title": "",
        "date_str": "",
        "news_type": "",
        "author": "",
        "node_id": "",
        "abstract": "",
        "subtitle": "",
    }

    try:
        soup = _make_soup(html)
    except Exception:
        return result
    if not soup:
        return result

    # Title: h2 with feature-title class (the article headline)
    title = ""
    for sel in ["h2.feature-title", ".node--type-news", ".l-image-header__content h2"]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                title = txt
                break
    result["title"] = title

    # Subtitle / deck
    subtitle_el = soup.select_one(".feature-subtitle")
    subtitle = subtitle_el.get_text(strip=True) if subtitle_el else ""
    result["subtitle"] = subtitle

    # Date + type from header superscript meta items
    meta_items = soup.select(".l-image-header__superscript .meta .meta__item")
    if not meta_items:
        meta_items = soup.select(".meta .meta__item")
    date_str = meta_items[0].get_text(strip=True) if len(meta_items) > 0 else ""
    news_type = meta_items[1].get_text(strip=True) if len(meta_items) > 1 else ""
    result["date_str"] = date_str
    result["news_type"] = news_type

    # Author: look for byline / author-named elements
    author = ""
    for sel in [
        ".field--name-field-byline",
        ".byline",
        "[class*=byline]",
        "[class*=author]",
        ".field--name-field-author",
    ]:
        el = soup.select_one(sel)
        if el:
            txt = el.get_text(strip=True)
            if txt:
                author = txt
                break
    # Heuristic fallback: scan header text for a personal name pattern
    if not author:
        header_el = soup.select_one(".l-image-header__main-inner")
        if header_el:
            header_text = header_el.get_text(separator="\n", strip=True)
            # Author appears after subtitle as lines not matching date/type/title text
            known = {title.lower(), subtitle.lower(), date_str.lower(), news_type.lower()}
            candidate_lines = []
            for line in header_text.split("\n"):
                line = line.strip().rstrip(",")
                if not line or line.lower() in known:
                    continue
                # Skip dates (contains digit + 4-digit year)
                if re.search(r"\d{4}", line) and re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\b", line, re.I):
                    continue
                # Skip type labels
                if line.lower() in ("news release", "feature", "staff accomplishment", "pnnl", "open"):
                    continue
                # Keep if it looks like a name (2+ words, mostly alphabetic)
                if re.match(r"^[A-Z][a-z]+ [A-Z]", line) and len(line) < 60:
                    candidate_lines.append(line)
            if candidate_lines:
                author = candidate_lines[0]
    result["author"] = author

    # Node ID from "nid" in Drupal settings JSON embedded in page
    nid_match = re.search(r'"nid"\s*:\s*(\d+)', html)
    if nid_match:
        result["node_id"] = nid_match.group(1)
    else:
        # fallback: currentPath node/NNN
        cp_match = re.search(r'"currentPath"\s*:\s*"node\\/(\d+)"', html)
        if not cp_match:
            cp_match = re.search(r'"currentPath"\s*:\s*"node/(\d+)"', html)
        if cp_match:
            result["node_id"] = cp_match.group(1)

    # Article body: .l-story__body with social share and boilerplate removed
    body = ""
    body_el = soup.select_one(".l-story__body")
    if body_el:
        for rm in body_el.select(".social-share, .social-links, .field--name-body"):
            rm.decompose()
        body = body_el.get_text(separator=" ", strip=True)

    # Build abstract: subtitle + body (both non-empty joined by double newline)
    parts = [p for p in (subtitle, body) if p]
    result["abstract"] = "\n\n".join(parts)

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class PNNLNewsGovCrawler(BaseCrawler):
    """Crawler for PNNL (Pacific Northwest National Laboratory) news."""

    site_id = "pnnl-gov-news"
    site_name = "Custom: pnnl-gov-news"
    base_url = "https://www.pnnl.gov"

    def crawl(self, limit=None) -> int:
        """Crawl PNNL Top Stories (type:23) via Drupal Views AJAX.

        Paginates through list pages, fetches each detail page for the full
        abstract, and saves via self._save_paper().  Respects limit, deduplication
        via seen_urls, a 200-page safety cap, and a 25-minute wall-clock budget.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "∞"

        page = 0
        while True:
            # --- Guard: wall-clock budget (25 min) ---
            if time.time() - start_time > 25 * 60:
                print(f"[pnnl-gov-news] 25-minute budget reached at page {page}, stopping")
                break

            # --- Guard: limit satisfied ---
            if limit is not None and saved >= limit:
                break

            # --- Guard: safety cap ---
            if page >= _SAFETY_CAP:
                print(f"[pnnl-gov-news] Safety cap of {_SAFETY_CAP} pages reached, stopping")
                break

            # --- Progress log every 10 pages ---
            if page > 0 and page % 10 == 0:
                print(f"[pnnl-gov-news] page {page}: saved {saved}/{limit_or_inf}")

            # --- Fetch list page ---
            raw = _curl_post_list(page=page)
            if not raw:
                print(f"[pnnl-gov-news] Failed to fetch page {page}, stopping")
                break

            cards = _parse_list_response(raw)
            if not cards:
                print(f"[pnnl-gov-news] No cards on page {page} — pagination complete")
                break

            # --- Deduplication ---
            new_cards = [c for c in cards if c["url"] not in seen_urls]
            for c in new_cards:
                seen_urls.add(c["url"])

            if not new_cards:
                print(f"[pnnl-gov-news] All items on page {page} already seen — pagination complete")
                break

            # --- Process each card ---
            for card in new_cards:
                if limit is not None and saved >= limit:
                    break

                item_url = card["url"]
                try:
                    time.sleep(self._delay)

                    detail_html = _curl_get(item_url)
                    if not detail_html:
                        print(f"[pnnl-gov-news] item {item_url} detail fetch failed, skipping")
                        continue

                    detail = _parse_detail(detail_html)

                    # Prefer detail-page title over list title (more authoritative)
                    title = detail["title"] or card["title"]
                    if not title:
                        print(f"[pnnl-gov-news] item {item_url} has no title, skipping")
                        continue

                    abstract = detail["abstract"]
                    if not abstract:
                        abstract = card.get("summary", "")

                    if len(abstract) < 50:
                        print(
                            f"[pnnl-gov-news] item {item_url} abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    date_str = detail["date_str"] or card["date_str"]
                    published_date = _parse_date(date_str)
                    news_type = detail["news_type"] or card["news_type"]
                    author = detail["author"]
                    node_id = detail["node_id"]
                    subtitle = detail["subtitle"]
                    slug = item_url.rstrip("/").split("/")[-1]

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": node_id or slug,
                        "post_number": node_id or None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "authors": author,
                        "publisher": "Pacific Northwest National Laboratory",
                        "department": "",
                        "journal": "",
                        "url": item_url,
                        "pdf_url": None,
                        "keywords": "",
                        "category": news_type,
                        "doi": "",
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_str,
                                "node_id": node_id,
                                "subtitle": subtitle,
                                "news_type": news_type,
                                "slug": slug,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[pnnl-gov-news] Saved {saved}/{limit_or_inf}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[pnnl-gov-news] item {item_url} failed: {exc}")
                    continue

            page += 1

        print(f"[pnnl-gov-news] Done. Total saved: {saved}")
        return saved
