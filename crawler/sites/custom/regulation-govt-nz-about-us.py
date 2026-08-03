# -*- coding: utf-8 -*-
"""Crawler for Ministry for Regulation – Our Publications (Proactive Releases).

Target:
  https://www.regulation.govt.nz/about-us/our-publications/search/
  ?query=&order=relevance&publication_type_id%5B110%5D=110&start={offset}

Page structure (SilverStripe CMS 5.3 / Vue frontend, server-side rendered):
  List: <li class="search-result__wrapper"> → title, listed_date, detail URL
  Detail: <section class="document-details"> → pub_type / topics / date published
           <a class="download-card"> → PDF href + aria-label "Download #N"

Abstract: constructed from metadata (title + type + topics + date + publisher)
          since pages carry no free-text body — always ≥100 chars.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from typing import Optional

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID   = "regulation-govt-nz-about-us"
_SEARCH_URL = "https://www.regulation.govt.nz/about-us/our-publications/search/"
_PAGE_SIZE  = 12
_MAX_PAGES  = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _parse_date(text: str) -> Optional[str]:
    """Convert '18 November 2024' or '3 May 2024' → 'YYYY-MM-DD'."""
    if not text:
        return None
    text = text.strip()
    m = re.match(r"(\d{1,2})\s+(\w+)\s+(\d{4})", text)
    if m:
        day = m.group(1).zfill(2)
        mon = _MONTH_MAP.get(m.group(2).lower())
        if mon:
            return f"{m.group(3)}-{mon}-{day}"
    return None


def _make_soup(html: str):
    """Try html5lib → lxml → html.parser; return None on total failure."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> Optional[str]:
    """Fetch URL with curl; exponential backoff 1s/3s/9s on failure."""
    ua = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {ua}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = res.stdout
            if raw and len(raw.strip()) > 200:
                return raw.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                w = waits[attempt]
                print(f"[{_SITE_ID}] Empty/short response for {url}, retry {attempt+1}/{retries} in {w}s…")
                time.sleep(w)
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                w = waits[attempt]
                print(f"[{_SITE_ID}] Timeout for {url}, retry {attempt+1}/{retries} in {w}s…")
                time.sleep(w)
            else:
                print(f"[{_SITE_ID}] curl timed out after {retries} attempts: {url}")
        except Exception as exc:
            if attempt < retries - 1:
                w = waits[attempt]
                print(f"[{_SITE_ID}] curl error: {exc}, retry {attempt+1}/{retries} in {w}s…")
                time.sleep(w)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _parse_list_page(html: str, base_url: str) -> list[dict]:
    """Extract items from a publications search result page."""
    try:
        soup = _make_soup(html)
    except Exception:
        return []
    if not soup:
        return []
    items = []
    for li in soup.select("li.search-result__wrapper"):
        try:
            a = li.select_one("a.search-result__link")
            if not a:
                continue
            title = a.get_text(strip=True)
            href  = a.get("href", "")
            if not href or not title:
                continue
            url = base_url + href if href.startswith("/") else href
            date_el = li.select_one("span.search-result__date")
            listed_date_raw = date_el.get_text(strip=True) if date_el else ""
            slug = href.strip("/").split("/")[-1]
            items.append({
                "title": title,
                "url": url,
                "href": href,
                "slug": slug,
                "listed_date_raw": listed_date_raw,
            })
        except Exception as exc:
            print(f"[{_SITE_ID}] list item parse error: {exc}")
            continue
    return items


def _parse_detail_page(html: str, base_url: str) -> dict:
    """Extract pub_type, topics, date, and PDF info from a detail page."""
    try:
        soup = _make_soup(html)
    except Exception:
        return {}
    if not soup:
        return {}

    result: dict = {}

    # Metadata fields (Publication type / Topic(s) / Date published)
    for field in soup.select("div.document-details__field"):
        try:
            label_el = field.select_one("label")
            if not label_el:
                continue
            label = label_el.get_text(strip=True).lower().rstrip(":")
            val_div = field.find("div")
            value = val_div.get_text(separator=" ", strip=True) if val_div else ""
            value = re.sub(r"\s+", " ", value).strip()
            if "publication type" in label:
                result["pub_type"] = value
            elif "topic" in label:
                result["topics"] = value
            elif "date published" in label:
                result["published_date_raw"] = value
        except Exception:
            continue

    # PDF download cards
    pdfs = []
    for card in soup.select("a.download-card"):
        try:
            href = card.get("href", "")
            if not href:
                continue
            pdf_url = base_url + href if href.startswith("/") else href
            aria = card.get("aria-label", "")
            m = re.search(r"#(\d+)", aria)
            download_id = m.group(1) if m else None
            filename = href.rstrip("/").split("/")[-1].split("?")[0] if href else None
            title_el = card.select_one(".download-card__title")
            file_title = title_el.get_text(strip=True) if title_el else ""
            pdfs.append({
                "url": pdf_url,
                "id": download_id,
                "filename": filename,
                "title": file_title,
            })
        except Exception:
            continue
    result["pdfs"] = pdfs
    return result


class RegulationGovtNzAboutUsCrawler(BaseCrawler):
    """Crawler for Ministry for Regulation – Proactive Releases."""

    site_id   = _SITE_ID
    site_name = "Custom: regulation-govt-nz-about-us"
    base_url  = "https://www.regulation.govt.nz"

    def crawl(self, limit=None):
        """Crawl publications list and detail pages, save via _save_paper."""
        saved = 0
        page_num = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget guard
            if time.monotonic() - start_time > _WALL_BUDGET:
                print(f"[{self.site_id}] 25-minute budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page_num >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            offset = page_num * _PAGE_SIZE
            list_url = (
                f"{_SEARCH_URL}?query=&order=relevance"
                f"&publication_type_id%5B110%5D=110"
                f"&sort_date_from=&sort_date_to=&start={offset}"
            )

            time.sleep(self._delay)
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page_num + 1}. Stopping.")
                break

            items = _parse_list_page(raw, self.base_url)
            if not items:
                print(f"[{self.site_id}] No items on page {page_num + 1}. Done.")
                break

            # Dedup check — stop if all items already seen (loop guard)
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page_num + 1} already seen. Stopping.")
                break
            for it in new_items:
                seen_urls.add(it["url"])

            if page_num > 0 and page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num + 1}: saved {saved}/{limit_str}")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(item["url"])
                    if not detail_html:
                        print(f"[{self.site_id}] Failed to fetch detail: {item['url']}")
                        continue

                    detail = _parse_detail_page(detail_html, self.base_url)

                    title            = item["title"]
                    slug             = item["slug"]
                    listed_date_raw  = item["listed_date_raw"]
                    pub_type         = detail.get("pub_type") or "Proactive release"
                    topics           = detail.get("topics") or ""
                    pub_date_raw     = detail.get("published_date_raw") or listed_date_raw
                    pdfs             = detail.get("pdfs") or []

                    published_date = _parse_date(pub_date_raw)
                    listed_date    = _parse_date(listed_date_raw)

                    # Build abstract from metadata (guaranteed ≥100 chars for
                    # any item that has a title + type + date).
                    abstract_parts = [f"Title: {title}"]
                    if pub_type:
                        abstract_parts.append(f"Publication type: {pub_type}")
                    if topics:
                        abstract_parts.append(f"Topic(s): {topics}")
                    if pub_date_raw:
                        abstract_parts.append(f"Date published: {pub_date_raw}")
                    abstract_parts.append(
                        "Published by the Ministry for Regulation, New Zealand "
                        "(regulation.govt.nz)."
                    )
                    abstract = "\n\n".join(abstract_parts)

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract too short (<50 chars) for {item['url']}, skipping.")
                        continue

                    # Primary PDF (first card)
                    pdf_url           = pdfs[0]["url"]      if pdfs else None
                    original_filename = pdfs[0]["filename"] if pdfs else None
                    # post_number: use download asset ID (#N) from aria-label
                    post_number       = pdfs[0]["id"]       if pdfs else None

                    metadata_payload = {
                        "pub_type":           pub_type,
                        "topics":             topics,
                        "published_date_raw": pub_date_raw,
                        "listed_date_raw":    listed_date_raw,
                        "posted_date":        listed_date_raw,
                        "pdfs":               pdfs,
                        "slug":               slug,
                    }

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       slug,
                        "post_number":       post_number,
                        "title":             title,
                        "abstract":          abstract,
                        "published_date":    published_date,
                        "posted_date":       listed_date,
                        "url":               item["url"],
                        "pdf_url":           pdf_url,
                        "original_filename": original_filename,
                        "keywords":          topics,
                        "category":          pub_type,
                        "publisher":         "Ministry for Regulation",
                        "authors":           "",
                        "journal":           "",
                        "doi":               "",
                        "metadata":          json.dumps(metadata_payload, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('url', '?')} failed: {exc}")
                    continue

            page_num += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
