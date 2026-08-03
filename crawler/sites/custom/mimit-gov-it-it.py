# -*- coding: utf-8 -*-
"""MIMIT (Ministero delle Imprese e del Made in Italy) publications crawler.

Target: https://www.mimit.gov.it/it/per-i-media/pubblicazioni
Pagination: ?start=N (10 items/page, Joomla CMS)
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.mimit.gov.it/it/per-i-media/pubblicazioni"
_BASE_URL = "https://www.mimit.gov.it"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 100
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


# ---------------------------------------------------------------------------
# BeautifulSoup helpers — fallback chain: html5lib → lxml → html.parser
# ---------------------------------------------------------------------------

def _make_soup(html):
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


def _soup_text(tag):
    if tag is None:
        return ""
    text = tag.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(html_str):
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z#\d]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MimitGovItCrawler(BaseCrawler):
    """Crawler for MIMIT Ministry publications (Joomla HTML list + detail pages)."""

    site_id = "mimit-gov-it-it"
    site_name = "Custom: mimit-gov-it-it"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl. Returns decoded text or None on all failures."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept-Language: it-IT,it;q=0.9,en;q=0.8",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.returncode == 0 and result.stdout.strip():
                    try:
                        return result.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return result.stdout.decode("utf-8", errors="replace")
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[mimit-gov-it-it] Empty response, retry {attempt+1}/{retries} in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[mimit-gov-it-it] curl error: {exc}, retry {attempt+1}/{retries} in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[mimit-gov-it-it] curl failed after {retries} attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html):
        """Return list of absolute detail-page URLs found on a list page."""
        urls = []
        soup = _make_soup(html)
        if soup:
            for li in soup.select("li.list-title"):
                a = li.find("a", href=True)
                if a and "/per-i-media/pubblicazioni/" in a["href"]:
                    href = a["href"]
                    if not href.startswith("http"):
                        href = _BASE_URL + href
                    urls.append(href)
        # Regex fallback
        if not urls:
            for href in re.findall(r'href="(/it/per-i-media/pubblicazioni/[^"?#]+)"', html):
                urls.append(_BASE_URL + href)
        return urls

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail_page(self, html, url):
        """Extract metadata dict from a publication detail page."""
        soup = _make_soup(html)

        # --- Title ---
        title = ""
        if soup:
            h1 = soup.find("h1")
            if h1:
                title = _soup_text(h1)
        if not title:
            m = re.search(r'<meta\s+property="og:title"\s+content="([^"]+)"', html)
            if m:
                title = m.group(1).strip()
        if not title:
            m = re.search(r"<title>([^<]+)</title>", html)
            if m:
                title = m.group(1).strip()

        # --- Article body (abstract) ---
        abstract = ""
        if soup:
            body_div = soup.find("div", attrs={"itemprop": "articleBody"})
            if body_div:
                abstract = _soup_text(body_div)
        if not abstract:
            m = re.search(
                r'itemprop="articleBody"[^>]*>(.*?)(?:</div>\s*<div[^>]*class="article-info|$)',
                html, re.DOTALL,
            )
            if m:
                abstract = _strip_html(m.group(1))

        # --- Joomla article ID ---
        article_id = None
        if soup:
            inp = soup.find("input", {"name": "article_id"})
            if inp:
                article_id = inp.get("value", "").strip() or None
        if not article_id:
            m = re.search(r'name="article_id"\s+[^>]*value="(\d+)"', html)
            if not m:
                m = re.search(r'value="(\d+)"\s+[^>]*name="article_id"', html)
            if m:
                article_id = m.group(1)
        # Try JSON-LD com_content article ID
        if not article_id:
            m = re.search(r'"@id":\s*"https://www\.mimit\.gov\.it/#/schema/com_content/article/(\d+)"', html)
            if m:
                article_id = m.group(1)

        # --- Published date ---
        published_date = ""
        if soup:
            # Prefer the canonical datePublished tag
            time_tag = soup.find("time", attrs={"itemprop": "datePublished"})
            if not time_tag:
                time_tag = soup.find("time", attrs={"datetime": True})
            if time_tag:
                raw_dt = time_tag.get("datetime", "")
                if raw_dt:
                    published_date = raw_dt[:10]
        if not published_date:
            m = re.search(r'datetime="(\d{4}-\d{2}-\d{2})', html)
            if m:
                published_date = m.group(1)

        # --- PDF URL and original filename ---
        pdf_url = None
        original_filename = None
        if soup:
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    if not href.startswith("http"):
                        href = _BASE_URL + href
                    pdf_url = href
                    original_filename = href.rstrip("/").split("/")[-1]
                    break
        if not pdf_url:
            m = re.search(r'href="([^"]*\.pdf)"', html, re.IGNORECASE)
            if m:
                href = m.group(1)
                if not href.startswith("http"):
                    href = _BASE_URL + href
                pdf_url = href
                original_filename = href.rstrip("/").split("/")[-1]

        # --- Slug (last URL path segment) ---
        slug = url.rstrip("/").split("/")[-1]

        return {
            "title": title,
            "abstract": abstract,
            "article_id": article_id,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "slug": slug,
            "url": url,
        }

    # ------------------------------------------------------------------
    # Main crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Walk list pages and fetch each publication detail page.

        Parameters
        ----------
        limit : int or None
            Maximum records to save (None = unlimited).
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()

        for page_num in range(_MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[mimit-gov-it-it] 25-min wall-clock budget reached. Stopping.")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            offset = page_num * _PAGE_SIZE
            list_url = f"{_LIST_URL}?start={offset}"

            if page_num > 0 and page_num % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[mimit-gov-it-it] page {page_num}: saved {saved}/{limit_str}")

            raw_list = self._curl_get(list_url)
            if not raw_list:
                print(f"[mimit-gov-it-it] Failed to fetch list page {page_num} (start={offset}). Stopping.")
                break

            item_urls = self._parse_list_page(raw_list)
            if not item_urls:
                print(f"[mimit-gov-it-it] No items at page {page_num} (start={offset}). Done.")
                break

            # Deduplication — detect silent pagination loop-back
            new_urls = [u for u in item_urls if u not in seen_urls]
            if not new_urls:
                print(f"[mimit-gov-it-it] All items on page {page_num} already seen. Stopping.")
                break
            seen_urls.update(new_urls)

            for item_url in new_urls:
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time > _MAX_WALL_SECONDS:
                    print(f"[mimit-gov-it-it] Wall-clock budget reached inside page loop. Stopping.")
                    break

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(item_url)
                    if not detail_raw:
                        print(f"[mimit-gov-it-it] Failed to fetch detail: {item_url}")
                        continue

                    detail = self._parse_detail_page(detail_raw, item_url)

                    title = detail.get("title", "").strip()
                    abstract = detail.get("abstract", "").strip()
                    article_id = detail.get("article_id") or detail.get("slug")
                    published_date = detail.get("published_date", "")
                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename")
                    slug = detail.get("slug", "")

                    if not title:
                        print(f"[mimit-gov-it-it] No title for {item_url}, skipping")
                        continue

                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[mimit-gov-it-it] Abstract too short "
                            f"({len(abstract)} chars) for: {title[:60]!r}, skipping"
                        )
                        continue

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": article_id or slug,
                        "post_number": article_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": "Ministero delle Imprese e del Made in Italy",
                        "category": "Pubblicazioni",
                        "keywords": None,
                        "authors": None,
                        "department": None,
                        "journal": None,
                        "doi": None,
                        "metadata": json.dumps({
                            "slug": slug,
                            "article_id": article_id,
                            "posted_date": published_date,
                            "originalFilename": original_filename,
                        }, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[mimit-gov-it-it] Saved {saved}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[mimit-gov-it-it] item failed ({item_url}): {exc}")
                    continue

        print(f"[mimit-gov-it-it] Done. Total saved: {saved}")
        return saved
