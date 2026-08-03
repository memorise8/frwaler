# -*- coding: utf-8 -*-
"""Australian Federal Police (AFP) search crawler.

Starting URL: https://afp.gov.au/search?keys=pdf&content_type_id=All
Pagination: ?content_type_id=All&keys=pdf&page=N  (0-indexed, ~4 pages)
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://afp.gov.au"
_SEARCH_URL = f"{_BASE}/search"
_SITE_ID = "afp-gov-au-search"


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    """Fetch URL via curl (TLS-max 1.3, follow redirects). Returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
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


def _parse_list_page(html: str) -> list:
    """Extract result items from the AFP search list page HTML.

    Returns list of dicts with keys: node_id, title, url, listed_date,
    category, snippet.
    """
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] list parse error: {exc}")
        return []

    items = []
    for row in soup.find_all("div", class_="views-row"):
        try:
            # Node ID lives on the inner node div
            node_div = row.find("div", attrs={"data-history-node-id": True})
            node_id = node_div["data-history-node-id"] if node_div else ""

            # Category from Drupal content-type class (e.g. node--type-landing-page)
            category = ""
            if node_div:
                cls = " ".join(node_div.get("class", []))
                m = re.search(r"node--type-([\w-]+)", cls)
                if m:
                    category = m.group(1).replace("-", " ").title()

            # Title and URL
            title_div = row.find("div", class_="search-result__title")
            if not title_div:
                continue
            link_tag = title_div.find("a")
            if not link_tag:
                continue
            href = link_tag.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href
            title = link_tag.get_text(strip=True)
            if not title:
                continue

            # Date — ISO datetime attribute on <time> in the side column
            time_tag = row.find("time", attrs={"datetime": True})
            listed_date = ""
            datetime_raw = ""
            if time_tag:
                datetime_raw = time_tag["datetime"]
                listed_date = datetime_raw[:10]  # "2023-07-10T..." → "2023-07-10"

            # Search excerpt (short snippet; detail page fetched later for full text)
            excerpt_div = row.find("div", class_="field--search-api-excerpt")
            snippet = excerpt_div.get_text(" ", strip=True) if excerpt_div else ""

            items.append({
                "node_id": node_id,
                "title": title,
                "url": href,
                "listed_date": listed_date,
                "datetime_raw": datetime_raw,
                "category": category,
                "snippet": snippet,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] row parse error: {exc}")
            continue

    return items


def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract abstract text and first PDF URL from an AFP detail page."""
    abstract = ""
    pdf_url = ""

    try:
        soup = _make_soup(html)

        # Prefer <main>; fall back to role="main", id="main-content", article, body
        main = (
            soup.find("main")
            or soup.find(attrs={"role": "main"})
            or soup.find(id="main-content")
            or soup.find("article")
            or soup.body
        )

        if main:
            # Strip chrome noise before text extraction
            for noise in main.find_all(
                ["nav", "header", "footer", "script", "style", "form", "aside"]
            ):
                noise.decompose()

            # First PDF link in the main area
            for a in main.find_all("a", href=True):
                h = a["href"]
                if ".pdf" in h.lower():
                    if not h.startswith("http"):
                        h = _BASE + h
                    pdf_url = h
                    break

            # Collect meaningful text blocks
            parts = []
            for el in main.find_all(["p", "li", "h1", "h2", "h3", "h4", "td", "dd"]):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 20:
                    parts.append(t)
            if parts:
                abstract = " ".join(parts)
            else:
                abstract = main.get_text(" ", strip=True)
            abstract = re.sub(r"\s+", " ", abstract).strip()

    except Exception as exc:
        print(f"[{_SITE_ID}] detail parse error ({page_url}): {exc}")
        # Fallback: regex on raw HTML
        try:
            abstract = re.sub(r"<[^>]+>", " ", html)
            abstract = re.sub(r"\s+", " ", abstract).strip()[:3000]
            m = re.search(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
            if m:
                pdf_url = m.group(1)
                if not pdf_url.startswith("http"):
                    pdf_url = _BASE + pdf_url
        except Exception:
            pass

    return {"abstract": abstract, "pdf_url": pdf_url}


class AFPGovAuSearchCrawler(BaseCrawler):
    """Crawler for Australian Federal Police (AFP) site-wide PDF search."""

    site_id = "afp-gov-au-search"
    site_name = "Custom: afp-gov-au-search"
    base_url = "https://afp.gov.au"

    def crawl(self, limit=None):
        """Crawl AFP search results for keyword 'pdf'.

        Paginates through ?content_type_id=All&keys=pdf&page=N (0-indexed).
        Fetches each detail page to get a full abstract and PDF URL.
        """
        saved = 0
        seen_urls: set = set()
        crawl_start = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(max_pages):
            if time.time() - crawl_start > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_display}")

            page_url = (
                f"{_SEARCH_URL}?content_type_id=All&keys=pdf&page={page_num}"
            )
            raw = _curl_get(page_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page_num}: failed to fetch. Stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no results. Done.")
                break

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
                        detail = {"abstract": item.get("snippet", ""), "pdf_url": ""}

                    abstract = detail["abstract"] or item.get("snippet", "")

                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] skipping (abstract <50 chars): "
                            f"{item['title'][:60]}"
                        )
                        continue

                    pdf_url = detail["pdf_url"] or None
                    node_id = item.get("node_id") or ""
                    listed_date = item.get("listed_date", "")

                    # original_filename: last path segment of PDF URL
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail and len(tail) <= 200:
                            original_filename = tail

                    paper = {
                        "site_id": self.site_id,
                        "external_id": node_id,
                        "post_number": node_id,
                        "url": item_url,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": listed_date,
                        "posted_date": listed_date,
                        "listed_date": listed_date,
                        "authors": "",
                        "publisher": "Australian Federal Police",
                        "department": "Australian Federal Police",
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "category": item.get("category", ""),
                        "keywords": "",
                        "doi": "",
                        "metadata": json.dumps(
                            {
                                "node_id": node_id,
                                "posted_date": listed_date,
                                "snippet": item.get("snippet", ""),
                                "category": item.get("category", ""),
                                "datetime_raw": item.get("datetime_raw", ""),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{_SITE_ID}] saved {saved}/{limit_display}: "
                        f"{item['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({item_url}): {exc}")
                    continue

            # End-of-pagination: no unseen records on this page
            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page_num}: all results already seen. Done.")
                break

        if page_num == max_pages - 1:
            print(f"[{_SITE_ID}] safety cap of {max_pages} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
