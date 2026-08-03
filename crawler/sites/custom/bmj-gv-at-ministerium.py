# -*- coding: utf-8 -*-
"""Crawler for Austrian Federal Ministry of Justice (BMJ) - Aktuelle Meldungen.

Target: https://www.bmj.gv.at/ministerium/aktuelle-meldungen.html
Pagination: ?page=N (10 items per page, ~15 pages)
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin, unquote

from crawler.base_crawler import BaseCrawler

_SITE_ID = "bmj-gv-at-ministerium"
_BASE_URL = "https://www.bmj.gv.at"
_LIST_URL = f"{_BASE_URL}/ministerium/aktuelle-meldungen.html"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_GERMAN_MONTHS = {
    "Januar": "01", "Februar": "02", "März": "03", "April": "04",
    "Mai": "05", "Juni": "06", "Juli": "07", "August": "08",
    "September": "09", "Oktober": "10", "November": "11", "Dezember": "12",
}


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL with curl; returns decoded text or None after retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: de,en;q=0.7",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace").strip()
            if raw:
                return raw
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error attempt {attempt + 1}/{retries}: {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1, 3, 9
            time.sleep(wait)
    return None


def _make_soup(raw: str):
    """Parse HTML with html5lib → lxml → html.parser fallback. Returns None on all failures."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _parse_german_date(text: str) -> str:
    """Convert '11. Mai 2026' → '2026-05-11', or '' on parse failure."""
    m = re.match(r"(\d{1,2})\.\s+(\w+)\s+(\d{4})", text.strip())
    if not m:
        return ""
    day, month_name, year = m.groups()
    month = _GERMAN_MONTHS.get(month_name, "")
    if not month:
        return ""
    return f"{year}-{month}-{day.zfill(2)}"


def _text(el) -> str:
    """Get stripped text from a BeautifulSoup element, or '' if None."""
    if el is None:
        return ""
    return el.get_text(separator=" ", strip=True)


def _strip_tags(html_str: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&[a-zA-Z]+;|&#x?[0-9a-fA-F]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class BmjGvAtMinisteriumCrawler(BaseCrawler):
    """Crawler for bmj.gv.at Aktuelle Meldungen."""

    site_id = "bmj-gv-at-ministerium"
    site_name = "Custom: bmj-gv-at-ministerium"
    base_url = "https://www.bmj.gv.at"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > _CRAWL_BUDGET_SECS:
                print(f"[{_SITE_ID}] 25-min budget reached ({elapsed:.0f}s). Stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_display}")

            list_url = f"{_LIST_URL}?page={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{_SITE_ID}] Failed to parse list page {page}: {exc}. Stopping.")
                break

            if not soup:
                print(f"[{_SITE_ID}] All parsers failed for list page {page}. Stopping.")
                break

            items = soup.select("li.overview-item")
            if not items:
                print(f"[{_SITE_ID}] No items on list page {page}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    link_el = item.select_one("a.card-link")
                    if not link_el:
                        continue
                    href = link_el.get("href", "")
                    if not href:
                        continue
                    item_url = urljoin(_BASE_URL, href)
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_on_page += 1

                    # Slug → external_id / post_number
                    slug = unquote(href.rstrip("/").rsplit("/", 1)[-1]).replace(".html", "")

                    # List-level fields
                    date_el = item.select_one("small.card-date")
                    listed_date_raw = _text(date_el)
                    listed_date = _parse_german_date(listed_date_raw) if listed_date_raw else ""

                    list_title = _text(item.select_one("h2.card-title-heading"))
                    list_abstract = _text(item.select_one("p.card-text"))

                    # Fetch detail page with retry
                    time.sleep(self._delay)
                    detail_raw = None
                    for attempt in range(3):
                        detail_raw = _curl_get(item_url)
                        if detail_raw:
                            break
                        if attempt < 2:
                            wait = 3 ** attempt
                            print(f"[{_SITE_ID}] Retrying detail {slug} ({attempt + 2}/3) in {wait}s...")
                            time.sleep(wait)

                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {slug} failed: could not fetch detail page")
                        continue

                    try:
                        detail_soup = _make_soup(detail_raw)
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {slug} failed: parse error: {exc}")
                        continue

                    if not detail_soup:
                        print(f"[{_SITE_ID}] item {slug} failed: all parsers failed")
                        continue

                    # Published date from <time datetime="YYYY-MM-DD">
                    time_el = detail_soup.select_one("time.datetime")
                    published_date = ""
                    if time_el:
                        published_date = time_el.get("datetime", "").strip()
                        if not published_date:
                            published_date = _parse_german_date(_text(time_el))

                    # Title from detail page (more reliable than list)
                    title_el = detail_soup.select_one("span.title")
                    title = _text(title_el) or list_title

                    # Build abstract: <p class="abstract"> + all <p> and <li> in main
                    abstract_parts: list[str] = []
                    content_el = detail_soup.find(id="content")
                    main_el = detail_soup.find("main") or content_el

                    if content_el:
                        abs_el = content_el.select_one("p.abstract")
                        if abs_el:
                            t = _text(abs_el)
                            if t:
                                abstract_parts.append(t)

                    if main_el:
                        for tag in main_el.find_all(["p", "li"]):
                            cls = tag.get("class") or []
                            if "abstract" in cls:
                                continue
                            t = _text(tag)
                            if t and t not in abstract_parts:
                                abstract_parts.append(t)

                    # Fallback to list abstract if detail yielded nothing
                    if not abstract_parts and list_abstract:
                        abstract_parts.append(list_abstract)

                    abstract = "\n\n".join(abstract_parts)

                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] Skipping {slug}: abstract too short ({len(abstract)} chars)")
                        continue

                    # PDF links
                    pdf_url = None
                    original_filename = None
                    for a_el in detail_soup.find_all("a", href=True):
                        href_val = a_el["href"]
                        if href_val.lower().endswith(".pdf"):
                            pdf_url = urljoin(_BASE_URL, href_val)
                            fname = unquote(href_val.rstrip("/").rsplit("/", 1)[-1])
                            original_filename = fname if fname else None
                            break

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": slug,
                        "post_number": slug,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date or listed_date,
                        "posted_date": listed_date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "authors": "",
                        "publisher": "Bundesministerium für Justiz",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": "Aktuelle Meldungen",
                        "doi": "",
                        "metadata": json.dumps({
                            "posted_date": listed_date_raw,
                            "originalFilename": original_filename,
                            "slug": slug,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item failed: {exc}")
                    continue

            # If no new items were seen on this page, we've looped back or exhausted
            if new_on_page == 0:
                print(f"[{_SITE_ID}] No new items on page {page}. Done.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
