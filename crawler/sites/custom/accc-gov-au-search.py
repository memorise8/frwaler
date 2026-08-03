# -*- coding: utf-8 -*-
"""ACCC (Australian Competition and Consumer Commission) search crawler.

Starting URL: https://www.accc.gov.au/search?query=pdf
Pagination: ?query=pdf&page=N  (0-indexed, ~602 pages, 10 results/page)
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.accc.gov.au"
_SEARCH_URL = f"{_BASE}/search"
_QUERY = "pdf"


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
    """Fetch URL via curl with TLS-max 1.3, retry on failure."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
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
                print(f"[accc-gov-au-search] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[accc-gov-au-search] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[accc-gov-au-search] curl failed after {retries} attempts: {exc}")
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> str:
    """Convert '28 Nov 2025' → '2025-11-28'. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%d %b %Y", "%d %B %Y", "%B %d, %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def _parse_list_page(html: str) -> list[dict]:
    """Extract result items from the search list page HTML."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[accc-gov-au-search] list parse error: {exc}")
        return []

    items = []
    for row in soup.find_all("div", class_="accc-search-result"):
        try:
            title_tag = row.find("h3") or row.find("h2")
            if not title_tag:
                continue
            link_tag = title_tag.find("a")
            if not link_tag:
                continue

            href = link_tag.get("href", "")
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href

            title = link_tag.get_text(strip=True)

            # Category from breadcrumb
            breadcrumb = row.find(class_="accc-search-result__breadcrumb")
            category = breadcrumb.get_text(" › ", strip=True) if breadcrumb else ""

            # Date and snippet from excerpt field
            date_str = ""
            snippet = ""
            excerpt_el = row.find(class_="views-field-search-api-excerpt")
            if excerpt_el:
                date_el = excerpt_el.find(class_="accc-search-result__date")
                if date_el:
                    date_str = date_el.get_text(strip=True)
                    date_el.decompose()
                snippet = excerpt_el.get_text(" ", strip=True)

            items.append({
                "title": title,
                "url": href,
                "date_raw": date_str,
                "category": category,
                "snippet": snippet,
            })
        except Exception as exc:
            print(f"[accc-gov-au-search] row parse error: {exc}")
            continue

    return items


def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract abstract and pdf_url from a detail page."""
    abstract = ""
    pdf_url = ""

    try:
        soup = _make_soup(html)

        # Find main content area
        main = (
            soup.find("main")
            or soup.find(role="main")
            or soup.find(id="main-content")
            or soup.find("div", class_="main-container")
            or soup.find("article")
            or soup.body
        )

        if main:
            # Remove nav/header/footer noise from the main block
            for tag in main.find_all(["nav", "header", "footer", "script", "style",
                                       "form", "aside"]):
                tag.decompose()

            # Collect paragraph-level text
            parts = []
            for el in main.find_all(["p", "li", "h1", "h2", "h3", "h4", "td"]):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 20:
                    parts.append(t)
            if parts:
                abstract = " ".join(parts)
            else:
                abstract = main.get_text(" ", strip=True)
            abstract = re.sub(r"\s+", " ", abstract).strip()

            # Find first PDF link
            for a in main.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf") or ".pdf" in href.lower():
                    if not href.startswith("http"):
                        href = _BASE + href
                    pdf_url = href
                    break

    except Exception as exc:
        print(f"[accc-gov-au-search] detail parse error ({page_url}): {exc}")
        # Fallback: plain regex on raw html
        try:
            abstract = _strip_tags(html)[:3000]
            m = re.search(r'href="([^"]+\.pdf[^"]*)"', html, re.I)
            if m:
                pdf_url = m.group(1)
                if not pdf_url.startswith("http"):
                    pdf_url = _BASE + pdf_url
        except Exception:
            pass

    return {"abstract": abstract, "pdf_url": pdf_url}


class ACCCGovAuSearchCrawler(BaseCrawler):
    """Crawler for ACCC (Australian Competition and Consumer Commission) search."""

    site_id = "accc-gov-au-search"
    site_name = "Custom: accc-gov-au-search"
    base_url = "https://www.accc.gov.au"

    def crawl(self, limit=None):
        """Crawl ACCC search results for 'pdf'.

        Paginates through ?query=pdf&page=N (0-indexed).
        Fetches each detail page to extract the full abstract.
        """
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute budget
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(max_pages):
            # Wall-clock budget check
            if time.time() - crawl_start > max_wall:
                print(f"[accc-gov-au-search] 25-minute wall clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            url = f"{_SEARCH_URL}?query={_QUERY}&page={page_num}"
            raw = _curl_get(url)
            if not raw:
                print(f"[accc-gov-au-search] page {page_num}: failed to fetch, skipping")
                continue

            items = _parse_list_page(raw)
            if not items:
                print(f"[accc-gov-au-search] page {page_num}: no results. Done.")
                break

            # Progress log every 10 pages
            if page_num % 10 == 0:
                print(f"[accc-gov-au-search] page {page_num}: "
                      f"saved {saved}/{limit_display}")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]

                # URL dedup to prevent looping
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    # Fetch detail page
                    detail_html = _curl_get(item_url)
                    if detail_html:
                        detail = _parse_detail_page(detail_html, item_url)
                    else:
                        detail = {"abstract": item.get("snippet", ""), "pdf_url": ""}

                    abstract = detail["abstract"] or item.get("snippet", "")

                    if len(abstract) < 50:
                        print(f"[accc-gov-au-search] skipping (abstract <50 chars): "
                              f"{item['title'][:60]}")
                        continue

                    pdf_url = detail["pdf_url"] or ""
                    published_date = _parse_date(item.get("date_raw", ""))

                    # external_id: URL path without domain
                    path = item_url.replace(_BASE, "").strip("/")
                    external_id = path or item_url

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": item["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": item.get("category", ""),
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "Australian Competition and Consumer Commission",
                        "metadata": json.dumps({
                            "snippet": item.get("snippet", ""),
                            "breadcrumb": item.get("category", ""),
                            "date_raw": item.get("date_raw", ""),
                            "search_page": page_num,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[accc-gov-au-search] saved {saved}/{limit_display}: "
                          f"{item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[accc-gov-au-search] item failed ({item_url}): {exc}")
                    continue

            # End-of-pagination: no new (unseen) records on this page
            if new_on_page == 0:
                print(f"[accc-gov-au-search] page {page_num}: "
                      f"all results already seen. Done.")
                break

        if page_num == max_pages - 1:
            print(f"[accc-gov-au-search] safety cap of {max_pages} pages reached.")

        print(f"[accc-gov-au-search] Done. Total saved: {saved}")
        return saved
