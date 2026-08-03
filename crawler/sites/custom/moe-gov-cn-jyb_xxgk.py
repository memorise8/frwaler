# -*- coding: utf-8 -*-
"""Crawler for MOE (中华人民共和国教育部) 部机关信息公开年度报告."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin, urlparse

from crawler.base_crawler import BaseCrawler

_SITE_ID = "moe-gov-cn-jyb_xxgk"
_BASE_URL = "http://www.moe.gov.cn"
_LIST_URL = "http://www.moe.gov.cn/jyb_xxgk/xxgk/nianbao/jiguan/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 50
_WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes in seconds


def _make_soup(html: str | bytes):
    """BeautifulSoup with parser fallback chain."""
    from bs4 import BeautifulSoup
    if isinstance(html, bytes):
        try:
            html = html.decode("utf-8", errors="replace")
        except Exception:
            html = str(html)
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with retries and exponential backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = 1 * (3 ** attempt)  # 1s, 3s, 9s
            print(f"[{_SITE_ID}] Retrying {url} in {wait}s...")
            time.sleep(wait)
    return None


def _strip_tags(html_fragment: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html_fragment, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_post_number(url: str) -> str | None:
    """Extract numeric article ID from MOE URL like /t20260130_1427903.html → '1427903'."""
    m = re.search(r"_(\d+)\.html", url)
    if m:
        return m.group(1)
    return None


def _extract_content(html: str) -> str:
    """Extract article body text with multiple fallback strategies."""
    # Strategy 1: TRS_Editor (modern MOE pages)
    idx = html.find("TRS_Editor")
    if idx != -1:
        chunk = html[idx:]
        text = _strip_tags(chunk)
        # Remove the "TRS_Editor" prefix itself
        text = re.sub(r"^TRS_Editor\s*", "", text)
        if len(text) >= _ABSTRACT_MIN_CHARS:
            return text

    # Strategy 2: id="content" div
    m = re.search(r'id=["\']content["\']', html)
    if m:
        chunk = html[m.start():]
        # Try to find end of content div
        end = chunk.find("</div>", 200)
        if end == -1:
            end = 20000
        text = _strip_tags(chunk[:end])
        if len(text) >= _ABSTRACT_MIN_CHARS:
            return text

    # Strategy 3: collect all <p> paragraphs that contain Chinese text
    soup = _make_soup(html)
    if soup:
        paras = []
        for p in soup.find_all("p"):
            t = p.get_text(" ", strip=True)
            if t and len(t) > 10 and re.search(r"[一-鿿]", t):
                paras.append(t)
        text = " ".join(paras)
        if len(text) >= _ABSTRACT_MIN_CHARS:
            return text

    # Strategy 4: entire body stripped
    text = _strip_tags(html)
    return text


def _parse_list_page(html: str, list_url: str) -> list[dict]:
    """Parse the list page and return item dicts with url, title, date."""
    items = []
    soup = _make_soup(html)
    if soup is None:
        return items

    # Find the <dl id="list"> container
    dl = soup.find("dl", id="list")
    if dl is None:
        # Fallback: any <dd> with an <a> link and date span
        dl = soup

    for dd in dl.find_all("dd"):
        a_tag = dd.find("a", href=True)
        if not a_tag:
            continue

        href = a_tag.get("href", "").strip()
        if not href or href.startswith("javascript"):
            continue

        # Resolve URL
        if href.startswith("http"):
            detail_url = href
        elif href.startswith("/"):
            detail_url = _BASE_URL + href
        else:
            detail_url = urljoin(list_url, href)

        title = a_tag.get("title", "").strip() or a_tag.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()

        # Date from span.x_gkndbg_pcdate (desktop) or span.x_gkndbg_mdate (mobile)
        date_span = dd.find("span", class_="x_gkndbg_pcdate")
        if not date_span:
            date_span = dd.find("span", class_="x_gkndbg_mdate")
        listed_date = date_span.get_text(strip=True) if date_span else ""

        if title and detail_url:
            items.append({
                "url": detail_url,
                "title": title,
                "listed_date": listed_date,
            })

    return items


class MoeGovCnJybXxgkCrawler(BaseCrawler):
    site_id = "moe-gov-cn-jyb_xxgk"
    site_name = "Custom: moe-gov-cn-jyb_xxgk"
    base_url = "http://www.moe.gov.cn"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay)

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # This site has all articles on a single list page (no paginator).
        # The page loop is kept for robustness / future-proofing.
        for page_num in range(1, _MAX_PAGES + 1):
            # Wall-clock budget check
            elapsed = time.time() - start_time
            if elapsed >= _WALL_CLOCK_BUDGET:
                print(f"[{_SITE_ID}] Wall-clock budget reached after {elapsed:.0f}s, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            # Log progress every 10 pages
            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_str}")

            # Fetch list page — only page 1 exists for this site
            if page_num == 1:
                list_url = _LIST_URL
            else:
                # No further pages exist; stop gracefully
                break

            print(f"[{_SITE_ID}] Fetching list page {page_num}: {list_url}")
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page_num}, stopping.")
                break

            items = _parse_list_page(raw, list_url)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page_num}, stopping.")
                break

            print(f"[{_SITE_ID}] Found {len(items)} items on page {page_num}")

            # Deduplicate and limit
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All items on page {page_num} already seen, stopping.")
                break

            for it in new_items:
                seen_urls.add(it["url"])

            for it in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    n = self._process_item(it, start_time)
                    if n:
                        saved += 1
                        print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {it['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed ({it.get('url', '?')}): {exc}")
                    continue

                time.sleep(self._delay)

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    def _process_item(self, it: dict, start_time: float) -> bool:
        """Fetch detail page, extract content, save. Returns True if saved."""
        url = it["url"]
        title = it["title"]
        listed_date = it.get("listed_date", "")

        post_number = _extract_post_number(url)
        external_id = post_number or urlparse(url).path.split("/")[-1].replace(".html", "")

        # Fetch detail page with retry
        detail_html = None
        for attempt in range(3):
            detail_html = _curl_get(url)
            if detail_html:
                break
            wait = 1 * (3 ** attempt)
            print(f"[{_SITE_ID}] Detail fetch retry {attempt + 1}/3 for {url}, wait {wait}s")
            time.sleep(wait)

        if not detail_html:
            print(f"[{_SITE_ID}] Failed to fetch detail page: {url}")
            return False

        # Extract content / abstract
        abstract = _extract_content(detail_html)

        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars) for {url}, skipping.")
            return False

        # Find any PDF attachment
        pdf_url = None
        pdf_match = re.search(r'href=["\']([^"\']*\.pdf)["\']', detail_html, re.I)
        if pdf_match:
            raw_pdf = pdf_match.group(1)
            pdf_url = raw_pdf if raw_pdf.startswith("http") else _BASE_URL + raw_pdf

        original_filename = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1] or None

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": listed_date,
            "listed_date": listed_date,
            "authors": None,
            "publisher": "教育部",
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "doi": None,
            "keywords": None,
            "category": "政府信息公开年度报告",
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "posted_date": listed_date,
                    "originalFilename": original_filename,
                    "column": "部机关信息公开年度报告",
                    "site": "中华人民共和国教育部政府门户网站",
                },
                ensure_ascii=False,
            ),
        }

        self._save_paper(paper)
        return True
