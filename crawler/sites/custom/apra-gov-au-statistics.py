# -*- coding: utf-8 -*-
"""Custom crawler for APRA Statistics (apra-gov-au-statistics).

Target: https://www.apra.gov.au/statistics
"""

import json
import subprocess
import time
from urllib.parse import urljoin, urlparse, unquote

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.apra.gov.au"
_LIST_URL = f"{_BASE}/statistics"
_MAX_PAGES = 200
_RATE_LIMIT = 1.0  # seconds between detail fetches
_BUDGET_SECS = 25 * 60  # 25 minutes


def _curl(url, retries=3):
    """Fetch URL via curl with TLS/retry. Returns decoded text or None."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk",
                    "-A", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "-L", "--max-time", "30", url,
                ],
                capture_output=True,
                timeout=60,
            )
            if result.returncode == 0 and result.stdout:
                try:
                    return result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[apra-gov-au-statistics] curl error (attempt {attempt + 1}/{retries}) {url}: {exc}")
        if attempt < retries - 1:
            time.sleep(3 ** attempt)  # 1s, 3s
    return None


def _bs(raw):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if not raw:
        return None
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_date(dt_str):
    """Extract YYYY-MM-DD from ISO datetime string like '2026-04-30T10:00:00+10:00'."""
    if not dt_str:
        return None
    try:
        return dt_str[:10]
    except Exception:
        return None


def _extract_filename(url):
    """Get the URL-decoded filename from the last path segment."""
    if not url:
        return None
    try:
        path = urlparse(url).path
        name = unquote(path.rstrip("/").split("/")[-1].split("?")[0])
        return name if name else None
    except Exception:
        return None


def _get_abstract(detail_soup):
    """Extract the publication description from a detail page.

    Tries in order:
    1. First <p> with >=50 chars in the main content area.
    2. Subsequent <p> tags (concatenated) until >=50 chars.
    3. <meta name="description"> content.
    Returns None if nothing meets the threshold.
    """
    main = (
        detail_soup.find("main")
        or detail_soup.find("div", class_=lambda c: c and "layout-container" in c)
        or detail_soup
    )

    paragraphs = main.find_all("p") if main else detail_soup.find_all("p")
    for ptag in paragraphs:
        text = ptag.get_text(strip=True)
        if len(text) >= 50:
            return text

    # Fallback: concatenate all non-trivial paragraph texts
    parts = [p.get_text(strip=True) for p in paragraphs if len(p.get_text(strip=True)) > 10]
    if parts:
        combined = " ".join(parts)
        if len(combined) >= 50:
            return combined

    # Last resort: meta description
    meta = detail_soup.find("meta", attrs={"name": "description"})
    if meta:
        content = meta.get("content", "").strip()
        if len(content) >= 50:
            return content

    return None


class APRAStatisticsCrawler(BaseCrawler):
    """Crawler for APRA Statistics publications listing."""

    site_id = "apra-gov-au-statistics"
    site_name = "Custom: apra-gov-au-statistics"
    base_url = "https://www.apra.gov.au"

    def crawl(self, limit=None):
        """Crawl APRA statistics listing pages and save publication records.

        Walks pages until saved >= limit, no new records, or safety cap.
        Returns the count of saved records.
        """
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.monotonic()

        for page in range(_MAX_PAGES):
            # 25-minute wall-clock budget
            if time.monotonic() - start_time > _BUDGET_SECS:
                print(f"[apra-gov-au-statistics] 25-minute budget reached at page {page}, stopping.")
                break

            if saved >= limit_or_inf:
                break

            # Progress log every 10 pages
            if page > 0 and page % 10 == 0:
                print(f"[apra-gov-au-statistics] page {page}: saved {saved}/{limit_or_inf}")

            # Fetch listing page (page 0 = no query param)
            list_url = f"{_LIST_URL}?page={page}" if page > 0 else _LIST_URL
            raw = _curl(list_url)
            if not raw:
                print(f"[apra-gov-au-statistics] Failed to fetch listing page {page}, stopping.")
                break

            soup = _bs(raw)
            if not soup:
                print(f"[apra-gov-au-statistics] Failed to parse listing page {page}, stopping.")
                break

            articles = soup.select("div.views-row article")
            if not articles:
                print(f"[apra-gov-au-statistics] No articles on page {page}, stopping.")
                break

            new_on_page = 0

            for art in articles:
                if saved >= limit_or_inf:
                    break

                try:
                    # --- List-level extraction ---
                    link_tag = art.select_one("a.tile__link-cover")
                    if not link_tag:
                        continue
                    href = link_tag.get("href", "").strip()
                    if not href:
                        continue

                    detail_url = urljoin(_BASE, href)

                    # URL deduplication
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    # Listed date (tile shows when the page was last updated/published)
                    tile_time = art.select_one(".tile__date time")
                    listed_date_raw = tile_time.get("datetime", "") if tile_time else ""
                    listed_date = _parse_date(listed_date_raw)

                    # Category label from tile
                    cat_tag = art.select_one(".tile__subject")
                    category = cat_tag.get_text(strip=True) if cat_tag else "Statistical publication"

                    # --- Detail page fetch ---
                    time.sleep(_RATE_LIMIT)
                    detail_raw = _curl(detail_url)
                    if not detail_raw:
                        print(f"[apra-gov-au-statistics] detail fetch failed: {detail_url}")
                        continue

                    detail_soup = _bs(detail_raw)
                    if not detail_soup:
                        print(f"[apra-gov-au-statistics] detail parse failed: {detail_url}")
                        continue

                    # Title from H1 (fallback to link text)
                    h1 = detail_soup.find("h1")
                    title = h1.get_text(strip=True) if h1 else link_tag.get_text(strip=True)
                    if not title:
                        print(f"[apra-gov-au-statistics] no title for {detail_url}, skipping")
                        continue

                    # Published date from <time datetime>
                    time_tag = detail_soup.find("time", attrs={"datetime": True})
                    published_date_raw = time_tag.get("datetime", "") if time_tag else ""
                    published_date = _parse_date(published_date_raw)

                    # Abstract
                    abstract = _get_abstract(detail_soup)
                    if not abstract or len(abstract) < 50:
                        print(f"[apra-gov-au-statistics] abstract too short (<50) for {detail_url}, skipping")
                        continue

                    # PDF URL: first .pdf href on the detail page
                    pdf_url = None
                    for atag in detail_soup.find_all("a", href=True):
                        href_a = atag["href"]
                        lower = href_a.lower()
                        if ".pdf" in lower:
                            pdf_url = href_a if href_a.startswith("http") else urljoin(_BASE, href_a)
                            break

                    original_filename = _extract_filename(pdf_url) if pdf_url else None

                    # Use slug as external_id / post_number
                    slug = href.strip("/").split("/")[-1] or href.strip("/")

                    metadata = {
                        "posted_date": listed_date_raw,
                        "published_date_raw": published_date_raw,
                        "category": category,
                    }
                    if original_filename:
                        metadata["originalFilename"] = original_filename

                    row_id = self._save_paper({
                        "site_id": self.site_id,
                        "external_id": slug,
                        "url": detail_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "publisher": "Australian Prudential Regulation Authority",
                        "category": category,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })

                    if row_id:
                        saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[apra-gov-au-statistics] item failed ({detail_url if 'detail_url' in dir() else '?'}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[apra-gov-au-statistics] No new items on page {page}, stopping.")
                break

        if page == _MAX_PAGES - 1:
            print(f"[apra-gov-au-statistics] Safety cap of {_MAX_PAGES} pages reached, stopping.")

        return saved
