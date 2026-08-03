# -*- coding: utf-8 -*-
"""Crawler for IRD (Institut de Recherche pour le Développement) English press releases.

Target: https://en.ird.fr/sections/release-press-office
Drupal 10 site — HTML list pages with ?page=N pagination.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://en.ird.fr/sections/release-press-office"
_BASE_URL = "https://en.ird.fr"
_MONTH_MAP = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}
_BS_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html):
    from bs4 import BeautifulSoup
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url, retries=3):
    """Fetch URL with curl; returns decoded text or None on failure."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "-sk", "--max-time", "30",
                    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "-H", "Accept-Language: en-US,en;q=0.9",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            wait = [1, 3, 9][min(attempt, 2)]
            print(f"[en-ird-fr-sections] Empty response (attempt {attempt + 1}/{retries}) "
                  f"for {url}, retrying in {wait}s...")
            time.sleep(wait)
        except Exception as exc:
            wait = [1, 3, 9][min(attempt, 2)]
            if attempt < retries - 1:
                print(f"[en-ird-fr-sections] curl error (attempt {attempt + 1}/{retries}): "
                      f"{exc}, retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[en-ird-fr-sections] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _parse_date(raw):
    """Parse various date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    # ISO: 2025-06-12 or 2025-06-12T...
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # DD/MM/YY or DD/MM/YYYY
    m = re.match(r"(\d{1,2})/(\d{2})/(\d{2,4})$", raw)
    if m:
        day, month, year = m.groups()
        if len(year) == 2:
            year = f"20{year}" if int(year) < 50 else f"19{year}"
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    # "7th May 2025" / "16th June 2025"
    m = re.match(r"(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})", raw, re.I)
    if m:
        day, month_name, year = m.groups()
        month_num = _MONTH_MAP.get(month_name.lower(), "01")
        return f"{year}-{month_num}-{day.zfill(2)}"
    return ""


def _strip_tags(html_str):
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_str or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


class IrdPressReleaseCrawler(BaseCrawler):
    """Crawls IRD English press-release section (Drupal 10, HTML pagination)."""

    site_id = "en-ird-fr-sections"
    site_name = "Custom: en-ird-fr-sections"
    base_url = "https://en.ird.fr"

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page_num):
        """Return (article_urls: list[str], has_next: bool)."""
        url = f"{_LIST_URL}?page={page_num}"
        html = _curl_get(url)
        if not html:
            return [], False

        soup = _make_soup(html)
        if not soup:
            return [], False

        urls = []
        for a in soup.find_all("a", class_=lambda c: c and "teaser" in c):
            href = a.get("href", "").strip()
            if not href or href.startswith("#"):
                continue
            if href.startswith("/"):
                href = _BASE_URL + href
            if not href.startswith("http"):
                continue
            urls.append(href)

        # Check for a link to the next page
        next_href = f"?page={page_num + 1}"
        has_next = bool(html.find(next_href) >= 0)

        return urls, has_next

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, url):
        """Fetch and parse one article. Returns dict or None."""
        html = _curl_get(url)
        if not html:
            return None

        soup = _make_soup(html)
        if not soup:
            return None

        # --- title ---
        title = ""
        og_title = soup.find("meta", property="og:title")
        if og_title:
            title = (og_title.get("content") or "").replace(" | Site Web IRD", "").strip()
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = h1.get_text(" ", strip=True)

        # --- date ---
        date_raw = ""
        date_el = soup.find(class_="article__date")
        if date_el:
            date_raw = date_el.get_text(strip=True)
        if not date_raw:
            time_el = soup.find("time")
            if time_el:
                date_raw = time_el.get("datetime") or time_el.get_text(strip=True)
        published_date = _parse_date(date_raw)

        # --- abstract ---
        abstract_parts = []
        og_desc = soup.find("meta", property="og:description")
        if og_desc:
            desc = (og_desc.get("content") or "").strip()
            if desc:
                abstract_parts.append(desc)

        for el in soup.find_all(class_=lambda c: c and "field--name-field-bloc-text" in str(c)):
            text = _strip_tags(str(el))
            if text and text not in abstract_parts:
                abstract_parts.append(text)

        for el in soup.find_all(class_=lambda c: c and "field--name-body" in str(c)):
            text = _strip_tags(str(el))
            if text and text not in abstract_parts:
                abstract_parts.append(text)

        abstract = "\n\n".join(abstract_parts)

        # --- node ID (post_number) ---
        node_id = None
        # Most reliable: drupalSettings.path.currentPath = "node/XXXXX"
        m = re.search(r'"currentPath"\s*:\s*"node\\/(\d+)"', html)
        if m:
            node_id = m.group(1)
        if not node_id:
            el = soup.find(attrs={"data-history-node-id": True})
            if el:
                node_id = el.get("data-history-node-id")
        if not node_id:
            m = re.search(r'"nid"\s*[=:]\s*"?(\d+)"?', html)
            if m:
                node_id = m.group(1)

        # --- PDF ---
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if href.lower().endswith(".pdf"):
                if href.startswith("/"):
                    href = _BASE_URL + href
                pdf_url = href
                original_filename = href.rstrip("/").split("/")[-1] or None
                break

        # --- keywords ---
        keywords = ""
        tag_field = soup.find(class_=lambda c: c and "field--name-field-tags" in str(c))
        if tag_field:
            tags = [t.get_text(strip=True) for t in tag_field.find_all("a")]
            keywords = ",".join(t for t in tags if t)

        # --- category ---
        category = ""
        cat_field = soup.find(class_=lambda c: c and "field--name-field-rubric" in str(c))
        if cat_field:
            category = cat_field.get_text(" ", strip=True)

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "node_id": node_id,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "category": category,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl IRD English press releases section.

        Walks ?page=0, ?page=1, … until limit is reached, the pager
        disappears, all URLs have been seen, or the 25-minute budget is
        exhausted.
        """
        saved = 0
        seen_urls = set()
        page = 0
        limit_or_inf = limit if limit is not None else "∞"
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        while page < MAX_PAGES:
            if time.time() - start_time > MAX_SECONDS:
                print(f"[en-ird-fr-sections] 25-minute budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[en-ird-fr-sections] page {page}: saved {saved}/{limit_or_inf}")

            article_urls, has_next = self._fetch_list_page(page)

            if not article_urls:
                print(f"[en-ird-fr-sections] No articles on page {page}. Done.")
                break

            new_on_page = 0
            for art_url in article_urls:
                if limit is not None and saved >= limit:
                    break
                if art_url in seen_urls:
                    continue
                seen_urls.add(art_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(art_url)
                    if not detail:
                        print(f"[en-ird-fr-sections] item {art_url} failed: no detail returned")
                        continue

                    title = detail.get("title") or ""
                    abstract = detail.get("abstract") or ""

                    if not title:
                        print(f"[en-ird-fr-sections] item {art_url} failed: missing title")
                        continue

                    if len(abstract) < 50:
                        print(f"[en-ird-fr-sections] item {art_url} skipped: "
                              f"abstract too short ({len(abstract)} chars)")
                        continue

                    node_id = detail.get("node_id")
                    external_id = node_id or art_url.rstrip("/").split("/")[-1]
                    published_date = detail.get("published_date") or ""

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": node_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": art_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "keywords": detail.get("keywords") or "",
                        "category": detail.get("category") or "",
                        "publisher": "IRD",
                        "authors": "",
                        "doi": "",
                        "department": "",
                        "journal": "",
                        "metadata": json.dumps(
                            {
                                "node_id": node_id,
                                "posted_date": published_date,
                                "originalFilename": detail.get("original_filename"),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[en-ird-fr-sections] saved {saved}/{limit_or_inf}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[en-ird-fr-sections] item {art_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[en-ird-fr-sections] All URLs on page {page} already seen. Done.")
                break

            if not has_next:
                print(f"[en-ird-fr-sections] No next page after page {page}. Done.")
                break

            page += 1

        if page >= MAX_PAGES:
            print(f"[en-ird-fr-sections] Safety cap of {MAX_PAGES} pages reached.")

        print(f"[en-ird-fr-sections] Done. Total saved: {saved}")
        return saved
