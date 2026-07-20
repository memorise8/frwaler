# -*- coding: utf-8 -*-
"""Finansinspektionen (FI) English reports crawler.

Target: https://www.fi.se/en/published/reports/reports/
Pagination: ?year=YYYY  (year selector links on the list page; oldest entry covers
            pre-2022 content; crawler also probes earlier years automatically).
Detail pages: full editor-content body + PDF link extraction.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime
from urllib.parse import urljoin, urlparse

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.fi.se/en/published/reports/reports/"
_PUBLISHER = "Finansinspektionen"
_MAX_SECONDS = 25 * 60  # 25-minute wall-clock budget


def _curl_get(url: str, max_time: int = 30) -> str | None:
    """GET via curl with TLS 1.3; retries 3× with exponential backoff (1s, 3s, 9s)."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", str(max_time),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=max_time + 10)
            raw = result.stdout
            if raw and raw.strip():
                return raw.decode("utf-8", errors="replace")
            if attempt < 2:
                print(f"[fi-se-en] empty response for {url}, retrying in {waits[attempt]}s")
                time.sleep(waits[attempt])
        except Exception as exc:
            if attempt < 2:
                print(f"[fi-se-en] curl error: {exc}, retrying in {waits[attempt]}s")
                time.sleep(waits[attempt])
            else:
                print(f"[fi-se-en] curl failed after 3 attempts for {url}: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback parser chain; returns BeautifulSoup or None."""
    try:
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
    except Exception:
        pass
    return None


def _text(tag) -> str:
    """Extract and normalize whitespace from a BS4 tag or None."""
    if tag is None:
        return ""
    try:
        return re.sub(r"\s+", " ", tag.get_text(" ", strip=True)).strip()
    except Exception:
        return ""


class FiSeEnCrawler(BaseCrawler):
    site_id = "fi-se-en"
    site_name = "Custom: fi-se-en"
    base_url = "https://www.fi.se"

    # ------------------------------------------------------------------
    # Year discovery
    # ------------------------------------------------------------------

    def _discover_years(self) -> list[int]:
        """Return year integers to probe, newest-first.

        Reads the year selector from the list page. Also probes up to 15
        years older than the oldest selector entry, stopping when we hit
        consecutive empty years in crawl().
        """
        html = _curl_get(_LIST_URL)
        if not html:
            current = datetime.now().year
            return list(range(current, 2005, -1))

        years_str = re.findall(r'href="\?year=(\d{4})"', html)
        if not years_str:
            current = datetime.now().year
            return list(range(current, 2005, -1))

        known = sorted({int(y) for y in years_str}, reverse=True)
        oldest = min(known)
        probed = list(known)
        for y in range(oldest - 1, max(oldest - 16, 2004), -1):
            probed.append(y)
        return probed

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _fetch_year_items(self, year: int) -> list[dict]:
        """Return list-level item dicts for one year page (newest-first)."""
        url = f"{_LIST_URL}?year={year}"
        html = _curl_get(url)
        if not html:
            return []

        soup = _make_soup(html)
        if soup is None:
            return []

        results_div = soup.find("div", id="results")
        if not results_div:
            return []

        items: list[dict] = []
        for item_div in results_div.find_all("div", class_="list-item"):
            try:
                h2 = item_div.find("h2")
                if not h2:
                    continue
                a_tag = h2.find("a")
                if not a_tag:
                    continue
                title = _text(a_tag)
                href = a_tag.get("href", "").strip()
                if not href:
                    continue

                detail_url = urljoin(self.base_url, href)

                date_span = item_div.find("span", class_="date")
                listed_date = _text(date_span) if date_span else ""

                intro_p = item_div.find("p", class_="introduction")
                intro = _text(intro_p) if intro_p else ""

                cats = [_text(a) for a in item_div.find_all("a", class_="categoryLink") if _text(a)]

                # external_id: "YYYY/slug" derived from URL path
                m = re.search(r"/reports/reports/(\d{4})/([^/]+)/?$", href)
                if m:
                    external_id = f"{m.group(1)}/{m.group(2)}"
                else:
                    parts = [p for p in urlparse(detail_url).path.split("/") if p]
                    external_id = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]

                items.append({
                    "title": title,
                    "url": detail_url,
                    "listed_date": listed_date,
                    "intro": intro,
                    "categories": cats,
                    "external_id": external_id,
                })
            except Exception as exc:
                print(f"[fi-se-en] list item parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch a detail page; return body text, pdf_url, original_filename."""
        html = _curl_get(url)
        if not html:
            return {}

        soup = _make_soup(html)
        if soup is None:
            return {}

        result: dict = {}

        # Full body from editor-content
        editor = soup.find("div", class_="editor-content")
        if editor:
            result["body"] = _text(editor)
        else:
            # Fall back to all non-trivial <p> tags
            paras = [_text(p) for p in soup.find_all("p") if len(_text(p)) > 30]
            result["body"] = " ".join(paras[:12])

        # PDF link — prefer fi.se-hosted, accept any .pdf
        pdf_url = None
        original_filename = None
        fi_pdf = None
        any_pdf = None
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if ".pdf" not in h.lower():
                continue
            full = h if h.startswith("http") else urljoin(self.base_url, h)
            if any_pdf is None:
                any_pdf = full
            if "fi.se" in full and fi_pdf is None:
                fi_pdf = full
        pdf_url = fi_pdf or any_pdf
        if pdf_url:
            fname = urlparse(pdf_url).path.rstrip("/").split("/")[-1].split("?")[0]
            if "." in fname:
                original_filename = fname

        result["pdf_url"] = pdf_url
        result["original_filename"] = original_filename
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        wall_start = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"
        consecutive_empty = 0

        years = self._discover_years()
        print(f"[fi-se-en] probing years: {years[:8]}{'...' if len(years) > 8 else ''}")

        for year in years:
            if limit is not None and saved >= limit:
                break
            if time.time() - wall_start > _MAX_SECONDS:
                print("[fi-se-en] wall-clock budget reached, exiting cleanly")
                break

            time.sleep(self._delay)
            print(f"[fi-se-en] year {year}: saved {saved}/{limit_str}")

            try:
                items = self._fetch_year_items(year)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[fi-se-en] error fetching year {year}: {exc}")
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    print("[fi-se-en] 2 consecutive empty/failed years, stopping probe")
                    break
                continue

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                consecutive_empty += 1
                if consecutive_empty >= 2:
                    print("[fi-se-en] 2 consecutive empty years, stopping probe")
                    break
                continue
            consecutive_empty = 0

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - wall_start > _MAX_SECONDS:
                    print("[fi-se-en] wall-clock budget reached mid-year, exiting cleanly")
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(url)

                    body = detail.get("body", "") or ""
                    intro = item.get("intro", "") or ""

                    # Abstract: prefer full body; pad with intro if body is short
                    if len(body) >= 100:
                        abstract = body
                    elif body:
                        abstract = (intro + " " + body).strip()
                    else:
                        abstract = intro

                    if len(abstract) < 50:
                        print(f"[fi-se-en] skipping (abstract {len(abstract)} chars): {url}")
                        continue

                    listed_date = item.get("listed_date", "")
                    categories = item.get("categories", [])
                    category = "; ".join(categories) if categories else ""
                    external_id = item["external_id"]
                    pdf_url = detail.get("pdf_url") or ""
                    original_filename = detail.get("original_filename")

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": listed_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": _PUBLISHER,
                        "department": "",
                        "journal": "",
                        "authors": "",
                        "category": category,
                        "keywords": "",
                        "doi": "",
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "originalFilename": original_filename,
                            "categories": categories,
                            "intro": intro,
                            "year_filter": year,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[fi-se-en] saved {saved}/{limit_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[fi-se-en] item {url} failed: {exc}")
                    continue

        print(f"[fi-se-en] done: saved {saved} records total")
        return saved
