# -*- coding: utf-8 -*-
"""BMVG (German Federal Ministry of Defence) — English press releases.

Starting URL: https://www.bmvg.de/en/press/all-press-releases

Discovery notes:
- List API: GET /service/queryL-list-filter/en/277064?targetView=asFilterableList&offset=N
  Returns a <template> element containing <article> items (6 per page).
  Pagination key: ?offset=N (discovered from app.bundle.js bw-search component).
- Detail page: full text is inside <div class="RichText">.
- External ID: trailing numeric segment of the URL slug (e.g. 5940746).
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Absolute import — spec_from_file_location has no package context,
# but the test inserts the project root into sys.path beforehand.
from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.bmvg.de/service/queryL-list-filter/en/277064"
_PAGE_SIZE = 6
_MAX_PAGES = 200
_BUDGET_SECS = 25 * 60  # 25 minutes


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with retry / exponential backoff. Returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "--compressed",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            print(f"[bmvg-de-en] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            print(f"[bmvg-de-en] retrying in {wait}s…")
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(dt_str: str) -> str:
    """Convert ISO datetime string → YYYY-MM-DD."""
    if not dt_str:
        return ""
    try:
        return datetime.fromisoformat(dt_str).strftime("%Y-%m-%d")
    except Exception:
        m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)
        return m.group(1) if m else ""


def _extract_post_number(url: str) -> str | None:
    """Extract the trailing numeric ID from a BMVG article URL.

    E.g. /en/some-title-5940746  →  '5940746'
    """
    m = re.search(r"-(\d{5,})(?:[/?#].*)?$", url.rstrip("/"))
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _parse_list_page(html: str) -> list[dict]:
    """Parse articles from the <template> block returned by the list API."""
    items = []

    # The list API wraps all <li><article> elements in a <template> tag.
    # Use BeautifulSoup with fallback.
    try:
        soup = _make_soup(html)
    except Exception:
        soup = None

    if soup:
        articles = soup.find_all("article", class_=re.compile(r"Teaser"))
    else:
        articles = []

    if not articles:
        # Regex fallback: pull each <article>…</article> block
        raw_blocks = re.findall(
            r"<article[^>]*class=['\"][^'\"]*Teaser[^'\"]*['\"][^>]*>(.*?)</article>",
            html, re.DOTALL | re.IGNORECASE,
        )
        for block in raw_blocks:
            item = _parse_article_block_regex(block)
            if item:
                items.append(item)
        return items

    for art in articles:
        try:
            # URL + title
            link = art.find("a", href=True)
            if not link:
                continue
            url = link["href"].strip()
            if not url.startswith("http"):
                url = "https://www.bmvg.de" + url
            title = link.get_text(" ", strip=True)

            # Teaser paragraph
            p = art.find("p")
            teaser = p.get_text(" ", strip=True) if p else ""

            # Date
            time_tag = art.find("time")
            listed_date = _parse_date(time_tag.get("datetime", "") if time_tag else "")

            # Category
            em = art.find("em", class_=re.compile(r"Tag"))
            if em:
                # strip the visually-hidden <span> that says "Kategorie"
                for sp in em.find_all("span", class_="isAural"):
                    sp.decompose()
                category = em.get_text(" ", strip=True)
            else:
                category = ""

            items.append({
                "url": url,
                "title": title,
                "teaser": teaser,
                "listed_date": listed_date,
                "category": category,
            })
        except Exception as exc:
            print(f"[bmvg-de-en] list-item parse error: {exc}")
            continue

    return items


def _parse_article_block_regex(block: str) -> dict | None:
    """Regex fallback for a single <article> block (without BS4)."""
    link_m = re.search(
        r'<a\s+href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL
    )
    if not link_m:
        return None
    url = link_m.group(1).strip()
    if not url.startswith("http"):
        url = "https://www.bmvg.de" + url
    title = _strip_tags(link_m.group(2)).strip()

    p_m = re.search(r"<p[^>]*>(.*?)</p>", block, re.DOTALL)
    teaser = _strip_tags(p_m.group(1)).strip() if p_m else ""

    time_m = re.search(r'<time[^>]*datetime="([^"]+)"', block)
    listed_date = _parse_date(time_m.group(1)) if time_m else ""

    em_m = re.search(r'<em[^>]*class="[^"]*Tag[^"]*"[^>]*>(.*?)</em>', block, re.DOTALL)
    if em_m:
        category = _strip_tags(re.sub(r"<span[^>]*isAural[^>]*>.*?</span>", "", em_m.group(1), flags=re.DOTALL)).strip()
    else:
        category = ""

    return {"url": url, "title": title, "teaser": teaser, "listed_date": listed_date, "category": category}


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail_page(html: str) -> dict:
    """Extract full-text abstract and metadata from a press release detail page."""
    result: dict = {}

    try:
        soup = _make_soup(html)
    except Exception:
        soup = None

    if soup:
        # Full text
        rich = soup.find("div", class_="RichText")
        if rich:
            result["abstract"] = rich.get_text(" ", strip=True)

        # Published date
        time_tag = soup.find("time")
        if time_tag:
            result["published_date"] = _parse_date(time_tag.get("datetime", ""))

        # Title from h1
        h1 = soup.find("h1")
        if h1:
            result["title"] = h1.get_text(" ", strip=True)

        # PDF links
        pdf_links = [
            a["href"] for a in soup.find_all("a", href=True)
            if a["href"].lower().endswith(".pdf")
        ]
        if pdf_links:
            result["pdf_url"] = pdf_links[0]
            result["original_filename"] = pdf_links[0].rstrip("/").split("/")[-1]

    else:
        # Pure-regex fallback
        m = re.search(r'class="RichText">(.*?)(?=<footer|<bw-share|</main)', html, re.DOTALL)
        if m:
            result["abstract"] = _strip_tags(m.group(1)).strip()

        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', html)
        if time_m:
            result["published_date"] = _parse_date(time_m.group(1))

        h1_m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
        if h1_m:
            result["title"] = _strip_tags(h1_m.group(1)).strip()

        pdf_links = re.findall(r'href="([^"]*\.pdf[^"]*)"', html, re.I)
        if pdf_links:
            result["pdf_url"] = pdf_links[0]
            result["original_filename"] = pdf_links[0].rstrip("/").split("/")[-1]

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class BmvgDeEnCrawler(BaseCrawler):
    """Crawler for BMVG English press releases."""

    site_id = "bmvg-de-en"
    site_name = "Custom: bmvg-de-en"
    base_url = "https://www.bmvg.de"

    def crawl(self, limit=None):
        """Crawl BMVG English press releases.

        Uses offset-based pagination against the list-filter API.
        Fetches detail pages for full-text abstracts.
        """
        saved = 0
        seen_urls: set[str] = set()
        offset = 0
        page = 0
        limit_or_inf = str(limit) if limit is not None else "∞"
        start_time = time.time()

        while True:
            # ---- guards ----
            if page >= _MAX_PAGES:
                print(f"[bmvg-de-en] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > _BUDGET_SECS:
                print(f"[bmvg-de-en] 25-minute budget exceeded ({elapsed/60:.1f} min). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[bmvg-de-en] page {page}: saved {saved}/{limit_or_inf}")

            # ---- fetch list ----
            list_url = f"{_LIST_URL}?targetView=asFilterableList&offset={offset}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[bmvg-de-en] Failed to fetch list at offset={offset}. Stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[bmvg-de-en] No items at offset={offset}. Done.")
                break

            # ---- dedup check ----
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[bmvg-de-en] All items at offset={offset} already seen. Done.")
                break

            # ---- per-item processing ----
            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                url = it["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    post_number = _extract_post_number(url)
                    external_id = post_number

                    # Rate-limit before detail fetch
                    time.sleep(self._delay)

                    detail_raw = _curl_get(url)

                    abstract = ""
                    published_date = it["listed_date"]
                    title = it["title"]
                    pdf_url = None
                    original_filename = None

                    if detail_raw:
                        detail = _parse_detail_page(detail_raw)
                        abstract = detail.get("abstract", "")
                        if detail.get("published_date"):
                            published_date = detail["published_date"]
                        if detail.get("title"):
                            title = detail["title"]
                        pdf_url = detail.get("pdf_url")
                        original_filename = detail.get("original_filename")

                    # Fall back to teaser if RichText missing
                    if len(abstract) < 50:
                        if len(it["teaser"]) >= 50:
                            abstract = it["teaser"]
                        else:
                            print(
                                f"[bmvg-de-en] item {url} has short abstract "
                                f"({len(abstract)} chars), skipping"
                            )
                            continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "authors": "",
                        "publisher": "German Federal Ministry of Defence",
                        "department": "",
                        "journal": "",
                        "category": it["category"],
                        "keywords": "",
                        "published_date": published_date,
                        "listed_date": it["listed_date"],
                        "url": url,
                        "pdf_url": pdf_url or "",
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": it["listed_date"],
                                "teaser": it["teaser"],
                                "originalFilename": original_filename,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[bmvg-de-en] Saved {saved}/{limit_or_inf}: {title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[bmvg-de-en] item {url} failed: {exc}")
                    continue

            offset += _PAGE_SIZE
            page += 1

        print(f"[bmvg-de-en] Done. Total saved: {saved}")
        return saved
