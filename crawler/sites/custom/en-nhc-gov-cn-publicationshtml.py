# -*- coding: utf-8 -*-
"""NHC English Publications crawler.

Starting URL: https://en.nhc.gov.cn/publications.html
Static page with ~9 items and no API pagination.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from urllib.parse import urljoin

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "en-nhc-gov-cn-publicationshtml"
_BASE_URL = "https://en.nhc.gov.cn"
_LIST_URL = f"{_BASE_URL}/publications.html"
_BS_PARSERS = ("html5lib", "lxml", "html.parser")
_MAX_WALL_SECONDS = 25 * 60


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback parsers; return BeautifulSoup or None."""
    for parser in _BS_PARSERS:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#39;", "'", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with retries and exponential backoff. Returns text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1s, 3s
                print(f"[{_SITE_ID}] Empty response for {url}, retrying in {wait}s...")
                time.sleep(wait)
        except Exception as e:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error: {e}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {e}")
    return None


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def _parse_enpproperty(html: str) -> dict:
    """Extract metadata from <!--enpproperty...--> embedded comment."""
    result: dict = {}
    m = re.search(r"<!--enpproperty(.*?)/enpproperty-->", html, re.DOTALL)
    if not m:
        return result
    raw = m.group(1)
    for tag in ("articleid", "date", "author", "title", "keyword", "nodeid", "nodename"):
        tm = re.search(rf"<{tag}>(.*?)</{tag}>", raw, re.DOTALL)
        if tm:
            result[tag] = tm.group(1).strip()
    return result


def _extract_abstract(html: str, max_chars: int = 8000) -> str:
    """Extract main article body text from a detail page."""
    # Primary: <!--enpcontent-->...<!--/enpcontent--> markers
    m = re.search(r"<!--enpcontent-->(.*?)<!--/enpcontent-->", html, re.DOTALL)
    if m:
        return _strip_tags(m.group(1))[:max_chars]

    # Fallback: art-text div via BeautifulSoup
    soup = _make_soup(html)
    if soup:
        art = soup.find("div", class_="art-text")
        if art:
            txt = art.get_text(separator=" ", strip=True)
            return re.sub(r"\s+", " ", txt).strip()[:max_chars]

    # Last resort: strip full page
    return _strip_tags(html)[:max_chars]


def _extract_pdf_url(html: str, page_url: str) -> str | None:
    """Return the first PDF URL found on the page, or None."""
    for m in re.finditer(r'href=["\']([^"\']+\.pdf)["\' >]', html, re.IGNORECASE):
        url = m.group(1)
        if url.startswith("http"):
            return url
        return urljoin(page_url, url)
    return None


def _parse_date(raw: str) -> str:
    """Extract YYYY-MM-DD from '2020-09-07 17:47:15.0' or similar."""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
    return m.group(1) if m else ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class NHCPublicationsCrawler(BaseCrawler):
    """Crawler for NHC English Publications."""

    site_id = "en-nhc-gov-cn-publicationshtml"
    site_name = "Custom: en-nhc-gov-cn-publicationshtml"
    base_url = "https://en.nhc.gov.cn"

    def crawl(self, limit=None):
        """Crawl https://en.nhc.gov.cn/publications.html.

        The page is a static list (~9 items) with no server-side pagination.
        We treat the single list page as page 1 and walk detail pages until
        limit is reached or items are exhausted.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        # ------------------------------------------------------------------ #
        # Page 1: fetch the list                                               #
        # ------------------------------------------------------------------ #
        p = 1
        print(f"[{_SITE_ID}] page {p}: fetching list {_LIST_URL}")
        raw = _curl_get(_LIST_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch list page. Aborting.")
            return 0

        # Parse all publication links from the list div
        items: list[dict] = []
        soup = _make_soup(raw)
        if soup:
            list_div = soup.find("div", class_="list")
            if list_div:
                for a in list_div.find_all("a", href=True):
                    href = a["href"].strip()
                    if not href or href.startswith("javascript"):
                        continue
                    full_url = (href if href.startswith("http")
                                else urljoin(_BASE_URL + "/", href))
                    title = a.get_text(strip=True)
                    if full_url not in seen_urls and title:
                        seen_urls.add(full_url)
                        items.append({"url": full_url, "title": title})

        if not items:
            # Regex fallback for robustness
            for m in re.finditer(
                r'<a\s[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                raw, re.DOTALL | re.IGNORECASE
            ):
                href = m.group(1).strip()
                text = _strip_tags(m.group(2))
                if not text or not href or href.startswith("javascript"):
                    continue
                if re.search(r"\d{4}-\d{2}/\d{2}/c_\d+\.htm", href):
                    full_url = (href if href.startswith("http")
                                else urljoin(_BASE_URL + "/", href))
                    if full_url not in seen_urls:
                        seen_urls.add(full_url)
                        items.append({"url": full_url, "title": text})

        print(f"[{_SITE_ID}] page {p}: found {len(items)} item(s) on list page")

        # Re-use seen_urls for detail-level deduplication too
        detail_seen: set[str] = set()

        # ------------------------------------------------------------------ #
        # Walk detail pages                                                    #
        # ------------------------------------------------------------------ #
        for idx, item in enumerate(items):
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{_SITE_ID}] Wall-clock budget reached. Stopping.")
                break

            detail_url = item["url"]
            list_title = item["title"]

            if detail_url in detail_seen:
                print(f"[{_SITE_ID}] Skipping duplicate URL: {detail_url}")
                continue
            detail_seen.add(detail_url)

            try:
                time.sleep(self._delay)
                detail_html = _curl_get(detail_url)
                if not detail_html:
                    print(f"[{_SITE_ID}] item {idx + 1} failed: empty response for {detail_url}")
                    continue

                # -- Metadata from embedded enpproperty comment --
                meta = _parse_enpproperty(detail_html)
                article_id = meta.get("articleid", "")
                raw_date = meta.get("date", "")
                title = (meta.get("title") or list_title or "").strip()
                keyword = (meta.get("keyword") or "").strip()
                node_id = meta.get("nodeid", "")

                published_date = _parse_date(raw_date)

                # -- Abstract --
                abstract = _extract_abstract(detail_html)
                if len(abstract) < 50:
                    print(
                        f"[{_SITE_ID}] item {idx + 1} abstract too short "
                        f"({len(abstract)} chars), skipping: {title[:60]}"
                    )
                    continue

                # -- PDF --
                pdf_url = _extract_pdf_url(detail_html, detail_url)
                original_filename = None
                if pdf_url:
                    original_filename = (
                        pdf_url.rstrip("/").split("/")[-1].split("?")[0] or None
                    )

                # -- external_id: prefer articleid, fall back to URL segment --
                external_id = article_id
                if not external_id:
                    m2 = re.search(r"c_(\d+)\.htm", detail_url)
                    external_id = m2.group(1) if m2 else detail_url

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": external_id,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "url": detail_url,
                    "pdf_url": pdf_url,
                    "original_filename": original_filename,
                    "keywords": keyword,
                    "authors": "",
                    "publisher": "National Health Commission of China",
                    "department": "",
                    "journal": "",
                    "category": "Publications",
                    "doi": None,
                    "metadata": json.dumps(
                        {
                            "posted_date": raw_date,
                            "node_id": node_id,
                            "articleid": article_id,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx + 1} failed: {exc}")
                continue

        # Progress log (pages — this site has only 1 list page)
        print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_str}")
        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
