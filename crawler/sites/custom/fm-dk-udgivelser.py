# -*- coding: utf-8 -*-
"""Finansministeriet (fm.dk) Udgivelser crawler.

Starting URL : https://fm.dk/udgivelser/
Pagination   : https://fm.dk/udgivelser/?pageNumber=N  (N >= 1)
~10 items/page, ~13 pages, SSR Umbraco HTML listing + detail pages.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://fm.dk"
_LIST_URL = "https://fm.dk/udgivelser/"
_PUBLISHER = "Finansministeriet"
_SITE_ID = "fm-dk-udgivelser"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HTML helpers
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
    """Fetch URL via curl with exponential-backoff retries.

    Returns decoded response text or None on failure.
    """
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
        "-H", f"Referer: {_BASE}/",
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


def _extract_pagination_max(html: str) -> int:
    """Return the highest page number found in pagination links."""
    pages = re.findall(r'[?&]pageNumber=(\d+)', html)
    return max((int(p) for p in pages), default=1)


def _extract_list_items(html: str) -> list:
    """Parse listing page HTML; return list of {url, title, listed_date, short_desc}."""
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[{_SITE_ID}] soup parse error on list page: {exc}")
        return []

    items = []
    for li in soup.select("li.results-list__item"):
        try:
            a = li.select_one("a[href]")
            if not a:
                continue
            href = a.get("href", "").strip()
            if not href:
                continue
            if href.startswith("/"):
                href = _BASE + href

            title_tag = li.select_one("h2.results-item__title")
            title = title_tag.get_text(strip=True) if title_tag else ""

            time_tag = li.select_one("time[datetime]")
            listed_date = time_tag.get("datetime", "").strip() if time_tag else ""

            desc_tag = li.select_one("p.results-item__description")
            short_desc = desc_tag.get_text(strip=True) if desc_tag else ""

            items.append({
                "url": href,
                "title": title,
                "listed_date": listed_date,
                "short_desc": short_desc,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] error parsing list item: {exc}")
            continue

    return items


def _fetch_detail(url: str) -> dict:
    """Fetch detail page; return {abstract, pdf_url, published_date, original_filename}."""
    result = {
        "abstract": "",
        "pdf_url": None,
        "published_date": "",
        "original_filename": None,
    }

    raw = _curl_get(url)
    if not raw:
        return result

    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] soup parse error on {url}: {exc}")
        return result

    # --- Abstract: intro paragraph + body text ---
    abstract_parts = []

    intro = soup.select_one("p.area-content__intro")
    if intro:
        text = intro.get_text(separator=" ", strip=True)
        if text:
            abstract_parts.append(text)

    body = soup.select_one("div.area-content__text")
    if body:
        text = body.get_text(separator=" ", strip=True)
        if text and text not in abstract_parts:
            abstract_parts.append(text)

    # Fallback: og:description meta tag
    if not abstract_parts:
        og = soup.select_one('meta[property="og:description"]')
        if og:
            text = og.get("content", "").strip()
            if text:
                abstract_parts.append(text)

    # Last resort: meta[name=description]
    if not abstract_parts:
        meta = soup.select_one('meta[name="description"]')
        if meta:
            text = meta.get("content", "").strip()
            if text:
                abstract_parts.append(text)

    result["abstract"] = "\n\n".join(abstract_parts)

    # --- Published date ---
    time_tag = soup.select_one("time.area-content__date-time[datetime]")
    if time_tag:
        result["published_date"] = time_tag.get("datetime", "").strip()
    else:
        time_tag = soup.select_one("time[datetime]")
        if time_tag:
            result["published_date"] = time_tag.get("datetime", "").strip()

    # --- PDF link: first .pdf href on the page ---
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        if href.lower().endswith(".pdf"):
            if href.startswith("/"):
                href = _BASE + href
            result["pdf_url"] = href
            parts = [p for p in href.rstrip("/").split("/") if p]
            result["original_filename"] = parts[-1] if parts else None
            break

    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class FMDKUdgivelserCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: fm-dk-udgivelser"
    base_url = _BASE

    def crawl(self, limit=None):
        """Crawl fm.dk/udgivelser and persist publications to the DB."""
        saved = 0
        seen_urls = set()
        page = 1
        max_page = None
        limit_or_inf = limit if limit is not None else "inf"
        start_time = time.time()
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        MAX_PAGES = 200

        while True:
            # Safety caps
            if page > MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_WALL_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded. Stopping.")
                break
            if limit is not None and saved >= limit:
                break

            if page > 1:
                time.sleep(self._delay)

            list_url = f"{_LIST_URL}?pageNumber={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch page {page}. Stopping.")
                break

            # Determine max page from first fetch
            if max_page is None:
                max_page = _extract_pagination_max(raw)
                print(f"[{_SITE_ID}] Total pages: {max_page}")

            items = _extract_list_items(raw)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items found. Done.")
                break

            # URL deduplication — detect pagination loop-back
            new_items = [it for it in items if it["url"] not in seen_urls]
            for it in items:
                seen_urls.add(it["url"])

            if not new_items:
                print(f"[{_SITE_ID}] page {page}: all items already seen. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                detail_url = item["url"]
                try:
                    time.sleep(self._delay)
                    detail = _fetch_detail(detail_url)

                    title = item["title"] or "(untitled)"
                    listed_date = item.get("listed_date", "")
                    published_date = detail.get("published_date") or listed_date
                    abstract = detail.get("abstract", "")

                    # Skip items with insufficient abstract
                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] skipping short abstract "
                              f"({len(abstract)} chars): {title[:60]}")
                        continue

                    # Derive identifiers from URL path
                    url_path = detail_url.replace(_BASE, "").rstrip("/")
                    slug = url_path.split("/")[-1] if "/" in url_path else url_path
                    external_id = url_path  # full relative path ensures uniqueness

                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename")

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": external_id,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "publisher": _PUBLISHER,
                        "authors": None,
                        "journal": None,
                        "keywords": None,
                        "doi": None,
                        "department": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "originalFilename": original_filename,
                            "short_description": item.get("short_desc", ""),
                            "slug": slug,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {detail_url} failed: {exc}")
                    continue

            # Stop after last known page
            if max_page is not None and page >= max_page:
                print(f"[{_SITE_ID}] Reached last page ({max_page}). Done.")
                break

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
