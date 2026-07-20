# -*- coding: utf-8 -*-
"""ALRC (Australian Law Reform Commission) crawler.

Starting URL: https://www.alrc.gov.au/?s=pdf&type=all
Pagination:   https://www.alrc.gov.au/page/N/?s=pdf&type=all  (N >= 2)
10 items/page, ~83 pages, ~827 results total.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.alrc.gov.au"
_PUBLISHER = "Australian Law Reform Commission"
_SITE_ID = "alrc-gov-au"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max 1.3 and exponential-backoff retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-AU,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _parse_date(raw: str) -> str:
    """Convert '21.11.2025', '21 Nov 2025', or '2025-11-21' → 'YYYY-MM-DD'.
    Returns raw string on failure (never crashes)."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%d.%m.%Y", "%d %b %Y", "%d %B %Y", "%B %d, %Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _extract_post_number(url: str) -> str | None:
    """Extract numeric post ID from URL path (e.g. /news/23188/ → '23188').
    Falls back to None if no 3+-digit segment found."""
    m = re.search(r"/(\d{3,})/?(?:[?#]|$)", url)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _parse_list_page(html: str) -> list[dict]:
    """Extract result items from the search list page."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] list parse error: {exc}")
        return []

    items = []
    for item in soup.find_all("div", class_="section-listing-item"):
        try:
            # Date listed on the search page
            date_el = item.find(class_="section-listing-item-date")
            date_raw = date_el.get_text(strip=True) if date_el else ""

            # Title + URL from h2 > a; some news items have empty anchor text
            link_tag = None
            h2 = item.find("h2")
            if h2:
                link_tag = h2.find("a", href=True)
            if not link_tag:
                link_tag = item.find("a", href=True)
            if not link_tag:
                continue

            href = link_tag.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href

            # Title: anchor text, then title= attr, then first paragraph
            title = link_tag.get_text(strip=True)
            if not title:
                title = link_tag.get("title", "").strip()

            # First non-trivial paragraph as snippet / fallback title
            snippet = ""
            for p in item.find_all("p"):
                t = p.get_text(" ", strip=True)
                if t and len(t) > 20:
                    snippet = t
                    break

            if not title:
                title = snippet[:80].strip()

            # Content-type label (Publications / News / Inquiries …)
            type_el = item.find(class_=lambda c: c and "type" in c if c else False)
            item_type = type_el.get_text(strip=True) if type_el else ""

            items.append({
                "title": title,
                "url": href,
                "date_raw": date_raw,
                "snippet": snippet,
                "item_type": item_type,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] row parse error: {exc}")
            continue

    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract title, abstract, pdf_url, date, keywords, category from detail page."""
    abstract = ""
    pdf_url = ""
    title = ""
    date_str = ""
    keywords = ""
    category = ""

    try:
        soup = _make_soup(html)

        # Title
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)

        # Date
        date_el = soup.find(class_=lambda c: c and "date" in c.lower() if c else False)
        if date_el:
            date_str = date_el.get_text(strip=True)

        # Main content
        main = (
            soup.find("main")
            or soup.find(id="main-content")
            or soup.find(class_="main-content")
            or soup.find("article")
            or soup.body
        )

        if main:
            # Remove noisy structural elements
            for tag in main.find_all(
                ["nav", "header", "footer", "script", "style", "form", "aside", "noscript"]
            ):
                tag.decompose()
            for tag in main.find_all(
                class_=lambda c: c and any(
                    x in c for x in ["widget", "sidebar", "breadcrumb", "social", "share", "related"]
                ) if c else False
            ):
                tag.decompose()

            # First PDF link
            for a in main.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    if not href.startswith("http"):
                        href = _BASE + href
                    pdf_url = href
                    break

            # Abstract: collect paragraphs/headings with substance
            parts = []
            for el in main.find_all(["p", "li", "h2", "h3", "h4", "h5"]):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 30:
                    parts.append(t)
            abstract = re.sub(r"\s+", " ", " ".join(parts)).strip() if parts else (
                re.sub(r"\s+", " ", main.get_text(" ", strip=True)).strip()
            )

            # Tags / keywords
            tags_el = main.find(class_=lambda c: c and "tag" in c.lower() if c else False)
            if tags_el:
                keywords = ", ".join(
                    a.get_text(strip=True) for a in tags_el.find_all("a") if a.get_text(strip=True)
                )

            # Category (filed-under / taxonomy)
            for cls_hint in ("filed", "categor", "taxonomy"):
                filed_el = main.find(
                    class_=lambda c: c and cls_hint in c.lower() if c else False
                )
                if filed_el:
                    category = filed_el.get_text(" ", strip=True)
                    break

    except Exception as exc:
        print(f"[{_SITE_ID}] detail parse error ({page_url}): {exc}")
        # Regex fallback — never let a parse error propagate
        try:
            abstract = re.sub(r"<[^>]+>", " ", html)
            abstract = re.sub(r"\s+", " ", abstract).strip()[:3000]
            m = re.search(r'href="([^"]+\.pdf[^"]*)"', html, re.IGNORECASE)
            if m:
                pdf_url = m.group(1)
                if not pdf_url.startswith("http"):
                    pdf_url = _BASE + pdf_url
        except Exception:
            pass

    return {
        "title": title,
        "abstract": abstract,
        "pdf_url": pdf_url,
        "date_str": date_str,
        "keywords": keywords,
        "category": category,
    }


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class ALRCGovAuCrawler(BaseCrawler):
    """Crawler for the Australian Law Reform Commission (alrc.gov.au)."""

    site_id = "alrc-gov-au"
    site_name = "Custom: alrc-gov-au"
    base_url = "https://www.alrc.gov.au"

    def crawl(self, limit=None):
        """Crawl ALRC search results for PDF-containing pages.

        Walks https://www.alrc.gov.au/page/N/?s=pdf&type=all until the limit is
        reached, no new items appear, or the 25-minute wall-clock budget expires.
        """
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        max_wall = 25 * 60  # seconds
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(1, max_pages + 1):
            if time.time() - crawl_start > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            url = (
                f"{self.base_url}/?s=pdf&type=all"
                if page_num == 1
                else f"{self.base_url}/page/{page_num}/?s=pdf&type=all"
            )

            raw = _curl_get(url)
            if not raw:
                print(f"[{_SITE_ID}] page {page_num}: failed to fetch, stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no results. Done.")
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

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
                    time.sleep(self._delay)

                    detail_html = _curl_get(item_url)
                    if detail_html:
                        detail = _parse_detail_page(detail_html, item_url)
                    else:
                        detail = {
                            "title": item.get("title", ""),
                            "abstract": item.get("snippet", ""),
                            "pdf_url": "",
                            "date_str": item.get("date_raw", ""),
                            "keywords": "",
                            "category": "",
                        }

                    title = detail["title"] or item.get("title", "")
                    abstract = detail["abstract"] or item.get("snippet", "")

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] skipping (abstract <50 chars): {title[:60]}")
                        continue

                    listed_date = _parse_date(item.get("date_raw", ""))
                    published_date = _parse_date(detail.get("date_str", "")) or listed_date

                    pdf_url = detail["pdf_url"] or ""
                    original_filename = ""
                    if pdf_url:
                        original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]

                    post_number = _extract_post_number(item_url)
                    external_id = item_url.replace(self.base_url, "").strip("/") or item_url

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": _PUBLISHER,
                        "journal": "",
                        "category": detail.get("category", "") or item.get("item_type", ""),
                        "keywords": detail.get("keywords", ""),
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": item.get("date_raw", ""),
                                "originalFilename": original_filename,
                                "item_type": item.get("item_type", ""),
                                "snippet": item.get("snippet", ""),
                                "search_page": page_num,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({item_url}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page_num}: all results already seen. Done.")
                break

        if page_num >= max_pages:
            print(f"[{_SITE_ID}] safety cap of {max_pages} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
