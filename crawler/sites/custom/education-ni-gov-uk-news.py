# -*- coding: utf-8 -*-
"""Department of Education Northern Ireland — news crawler.

Starting URL: https://www.education-ni.gov.uk/news
Pagination:   /news (page 0) then /news?page=1, /news?page=2, … (20/page)
~922 total results across ~47 pages.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.education-ni.gov.uk"
_SITE_ID = "education-ni-gov-uk-news"
_PUBLISHER = "Department of Education Northern Ireland"


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


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3):
    """Fetch URL via curl with TLS-max 1.3 and exponential-backoff retries.
    Returns decoded str or None on total failure."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-GB,en;q=0.9",
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


def _parse_datetime(raw: str) -> str:
    """Convert ISO datetime or human date string → 'YYYY-MM-DD'. Never crashes."""
    if not raw:
        return ""
    raw = raw.strip()
    # ISO datetime: 2026-05-13T12:00:00Z or plain 2026-05-13
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", raw)
    if m:
        return m.group(1)
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw


def _extract_node_id(html: str):
    """Extract Drupal node ID from settings JSON (currentPath: node/NNNNN)."""
    m = re.search(r'"currentPath"\s*:\s*"node(?:\\?/)(\d+)"', html)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# List-page parser
# ---------------------------------------------------------------------------

def _parse_list_page(html: str) -> list:
    """Return list of {title, url, datetime_attr, date_text, snippet} dicts."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] list parse error: {exc}")
        return []

    items = []
    # Cards may have multiple CSS classes; we match any <a> with class containing "card"
    for card in soup.find_all("a", href=True):
        cls = card.get("class") or []
        if "card" not in cls:
            continue
        try:
            href = card.get("href", "").strip()
            if not href:
                continue
            if not href.startswith("http"):
                href = _BASE + href
            # Only include /news/ detail pages (not category or section pages)
            if "/news/" not in href:
                continue

            h3 = card.find("h3", class_="card__title")
            title = h3.get_text(strip=True) if h3 else ""

            time_el = card.find("time")
            datetime_attr = time_el.get("datetime", "") if time_el else ""
            date_text = time_el.get_text(strip=True) if time_el else ""

            snippet = ""
            summary_div = card.find("div", class_="card__summary")
            if summary_div:
                for p in summary_div.find_all("p"):
                    t = p.get_text(" ", strip=True)
                    if len(t) > 20:
                        snippet = t
                        break

            items.append({
                "title": title,
                "url": href,
                "datetime_attr": datetime_attr,
                "date_text": date_text,
                "snippet": snippet,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] card parse error: {exc}")
            continue

    return items


# ---------------------------------------------------------------------------
# Detail-page parser
# ---------------------------------------------------------------------------

def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract title, abstract, published_date, node_id, pdf_url, keywords."""
    result = {
        "title": "",
        "abstract": "",
        "published_date": "",
        "node_id": "",
        "pdf_url": "",
        "keywords": "",
        "meta_description": "",
    }

    result["node_id"] = _extract_node_id(html) or ""

    # OG meta description as fallback abstract
    m = re.search(r'<meta name="description" content="([^"]+)"', html)
    if m:
        result["meta_description"] = m.group(1)

    # Published time from OG article meta
    m = re.search(r'article:published_time["\s]+content="([^"]+)"', html)
    if m:
        result["published_date"] = _parse_datetime(m.group(1))

    # Keywords from OG article:tag (may be empty strings — filter those out)
    tags = re.findall(r'<meta property="article:tag" content="([^"]*)"', html)
    result["keywords"] = ", ".join(t.strip() for t in tags if t.strip())

    try:
        soup = _make_soup(html)

        # Title
        h1 = soup.find("h1", class_="page-title") or soup.find("h1")
        if h1:
            result["title"] = h1.get_text(strip=True)

        # Published date fallback: <p class="published-date"> … <time datetime=…>
        if not result["published_date"]:
            p_date = soup.find("p", class_="published-date")
            if p_date:
                time_el = p_date.find("time")
                if time_el:
                    result["published_date"] = _parse_datetime(
                        time_el.get("datetime", "") or time_el.get_text(strip=True)
                    )

        # Main content container
        article = (
            soup.find("article", class_=lambda c: c and "article-content" in c if c else False)
            or soup.find("article")
            or soup.find("main")
            or soup.find(id="main-content")
        )

        if article:
            # Strip non-content noise
            for tag in article.find_all(
                ["nav", "header", "footer", "script", "style",
                 "form", "noscript", "svg", "aside"]
            ):
                tag.decompose()
            for tag in article.find_all(
                class_=lambda c: c and any(
                    x in c for x in [
                        "breadcrumb", "social", "share", "related",
                        "sidebar", "widget", "footer",
                    ]
                ) if c else False
            ):
                tag.decompose()

            # First PDF link
            for a in article.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    if not href.startswith("http"):
                        href = _BASE + href
                    result["pdf_url"] = href
                    break

            # Abstract: collect all substantive text nodes
            parts = []
            for el in article.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 20:
                    parts.append(t)
            if parts:
                result["abstract"] = re.sub(r"\s+", " ", " ".join(parts)).strip()
            else:
                result["abstract"] = re.sub(
                    r"\s+", " ", article.get_text(" ", strip=True)
                ).strip()

    except Exception as exc:
        print(f"[{_SITE_ID}] detail parse error ({page_url}): {exc}")
        # Regex strip fallback — never let a parse error stop the crawl
        try:
            result["abstract"] = re.sub(r"<[^>]+>", " ", html)
            result["abstract"] = re.sub(r"\s+", " ", result["abstract"]).strip()[:5000]
        except Exception:
            pass

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class EducationNIGovUKNewsCrawler(BaseCrawler):
    """Crawler for Department of Education Northern Ireland news."""

    site_id = "education-ni-gov-uk-news"
    site_name = "Custom: education-ni-gov-uk-news"
    base_url = "https://www.education-ni.gov.uk"

    def crawl(self, limit=None):
        """Walk /news listing pages and save detail-page content.

        Iterates /news (page 0) then /news?page=1, /news?page=2, … until
        ``limit`` is reached, the page yields no new items, or the 25-minute
        wall-clock budget or 200-page safety cap is hit.
        """
        saved = 0
        seen_urls: set = set()
        crawl_start = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
        max_pages = 200
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(0, max_pages):
            # Wall-clock budget check
            if time.time() - crawl_start > max_wall:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            list_url = (
                f"{self.base_url}/news"
                if page_num == 0
                else f"{self.base_url}/news?page={page_num}"
            )

            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] page {page_num}: failed to fetch listing, stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] page {page_num}: no items found. Done.")
                break

            if page_num > 0 and page_num % 10 == 0:
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
                        # Degrade gracefully: use listing data only
                        detail = {
                            "title": item.get("title", ""),
                            "abstract": item.get("snippet", ""),
                            "published_date": _parse_datetime(item.get("datetime_attr", "")),
                            "node_id": "",
                            "pdf_url": "",
                            "keywords": "",
                            "meta_description": item.get("snippet", ""),
                        }

                    title = detail["title"] or item.get("title", "")
                    abstract = detail["abstract"]

                    # Fallback: meta description is usually ~150 chars
                    if len(abstract) < 50 and detail.get("meta_description"):
                        abstract = detail["meta_description"]

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] skipping (abstract <50 chars): {title[:60]}")
                        continue

                    listed_date = _parse_datetime(item.get("datetime_attr", ""))
                    published_date = detail["published_date"] or listed_date

                    node_id = detail.get("node_id", "")
                    # external_id = URL slug (last path component)
                    slug = item_url.rstrip("/").rsplit("/", 1)[-1]
                    external_id = slug

                    pdf_url = detail.get("pdf_url", "")
                    original_filename = ""
                    if pdf_url:
                        original_filename = (
                            pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        )

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id or None,
                        "title": title,
                        "abstract": abstract,
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": _PUBLISHER,
                        "journal": "",
                        "category": "news",
                        "keywords": detail.get("keywords", ""),
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": item.get("date_text", ""),
                                "originalFilename": original_filename,
                                "node_id": node_id,
                                "snippet": item.get("snippet", ""),
                                "list_page": page_num,
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

            # End-of-pagination: page yielded no new (unseen) items
            if new_on_page == 0:
                print(f"[{_SITE_ID}] page {page_num}: all results already seen. Done.")
                break

        if page_num >= max_pages - 1:
            print(f"[{_SITE_ID}] safety cap of {max_pages} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
