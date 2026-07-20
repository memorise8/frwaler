# -*- coding: utf-8 -*-
"""EPA Ireland News Releases crawler.

Starting URL: https://www.epa.ie/news-releases/news-releases-2025/
Pagination: year-based pages (2026, 2025, 2024, 2023) discovered from sidebar.
Each year page lists all releases for that year in <p class="news-item-plain"> tags.
Detail pages contain the full press-release body text used as abstract.
"""

import json
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402  absolute import


class EPAIENewsReleasesCrawler(BaseCrawler):
    site_id = "epa-ie-news-releases"
    site_name = "Custom: epa-ie-news-releases"
    base_url = "https://www.epa.ie"

    _START_URL = "https://www.epa.ie/news-releases/news-releases-2025/"
    _PUBLISHER = "Environmental Protection Agency"
    _PAGE_SAFETY_CAP = 200
    _CRAWL_BUDGET_SECS = 25 * 60

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential backoff. Returns decoded text or None."""
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk",
                        "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                print(f"[epa-ie-news-releases] empty response attempt {attempt + 1}/3 for {url}")
            except Exception as exc:
                print(f"[epa-ie-news-releases] curl error attempt {attempt + 1}/3: {exc}")
            if attempt < 2:
                time.sleep(waits[attempt])
        print(f"[epa-ie-news-releases] all 3 attempts failed for {url}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        """Parse HTML with html5lib → lxml → html.parser fallback chain."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _strip_html(raw: str) -> str:
        """Strip HTML tags and decode common HTML entities."""
        _entities = {
            "&amp;": "&", "&lt;": "<", "&gt;": ">",
            "&quot;": '"', "&apos;": "'",
            "&euro;": "€", "&nbsp;": " ",
            "&rsquo;": "’", "&lsquo;": "‘",
            "&rdquo;": "”", "&ldquo;": "“",
            "&ndash;": "–", "&mdash;": "—",
            "&hellip;": "…",
        }
        text = re.sub(r"<[^>]+>", " ", raw)
        for ent, ch in _entities.items():
            text = text.replace(ent, ch)
        text = re.sub(r"&#\d+;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_list_date(raw: str) -> str:
        """'Dec 12 2025, 11:50 AM'  →  '2025-12-12'."""
        date_part = re.sub(r",\s*\d+:\d+.*$", "", raw.strip()).strip()
        for fmt in ("%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
            try:
                return datetime.strptime(date_part, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return date_part

    @staticmethod
    def _parse_detail_date(raw: str) -> str:
        """'December 12, 2025'  →  '2025-12-12'."""
        raw = raw.strip()
        for fmt in ("%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d %b %Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass
        return raw

    # ------------------------------------------------------------------
    # Page parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list:
        """Return list of item dicts from a year index page."""
        items = []
        pattern = re.compile(
            r'<p\s+class="news-item-plain">'
            r'<span\s+id="(d\.en\.(\d+))"></span>\s*'
            r'<strong><a\s+href="([^"]+)">(.*?)</a>\s*-\s*<span>([^<]+)</span>'
            r'</strong>\s*<br[^>]*>\s*(.*?)\s*</p>',
            re.DOTALL | re.IGNORECASE,
        )
        for m in pattern.finditer(html):
            full_id, num_id, url, title_raw, date_raw, abstract_raw = m.groups()
            items.append({
                "external_id": num_id,
                "post_number": num_id,
                "url": url.strip(),
                "title": self._strip_html(title_raw),
                "listed_date": self._parse_list_date(date_raw),
                "abstract_short": self._strip_html(abstract_raw),
            })
        return items

    def _get_year_pages(self, html: str) -> list:
        """Extract year page URLs from the sidebar nav, newest first."""
        urls = re.findall(
            r'href="(https://www\.epa\.ie/news-releases/news-releases-\d{4}/)"',
            html,
        )
        seen = set()
        result = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                result.append(u)
        return result

    def _parse_detail_page(self, html: str) -> dict:
        """Extract title, published_date, full abstract and optional pdf_url from a detail page."""
        result = {
            "title": "",
            "published_date": "",
            "abstract": "",
            "pdf_url": None,
            "original_filename": None,
        }

        soup = self._make_soup(html)
        if soup is None:
            return result

        # Title
        h1 = soup.find("h1", class_="news__title")
        if h1:
            result["title"] = h1.get_text(separator=" ", strip=True)

        # Date
        date_p = soup.find("p", class_="news__date")
        if date_p:
            span = date_p.find("span")
            if span:
                result["published_date"] = self._parse_detail_date(span.get_text(strip=True))

        # Full body content — the news div holds the press-release body
        news_div = soup.find("div", class_="news general-content")
        if not news_div:
            # Fallback: inner__main content area
            news_div = soup.find("div", class_="inner__main")
        if news_div:
            body_text = news_div.get_text(separator=" ", strip=True)
            result["abstract"] = re.sub(r"\s+", " ", body_text).strip()

            # EPA-hosted PDF links only
            for a_tag in news_div.find_all("a", href=True):
                href = a_tag["href"]
                if "epa.ie" in href and href.lower().endswith(".pdf"):
                    result["pdf_url"] = href
                    tail = href.rstrip("/").split("/")[-1].split("?")[0]
                    result["original_filename"] = tail if tail else None
                    break

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_urls = set()

        # Fetch starting year page; use it to discover all year pages from sidebar
        print(f"[epa-ie-news-releases] fetching start page: {self._START_URL}")
        start_html = self._curl_get(self._START_URL)
        if not start_html:
            print("[epa-ie-news-releases] failed to fetch start page, aborting")
            return 0

        year_pages = self._get_year_pages(start_html)
        if not year_pages:
            year_pages = [self._START_URL]
        print(f"[epa-ie-news-releases] discovered {len(year_pages)} year pages: {year_pages}")

        page_num = 0
        for year_url in year_pages:
            if limit is not None and saved >= limit:
                break
            if page_num >= self._PAGE_SAFETY_CAP:
                print(f"[epa-ie-news-releases] safety cap of {self._PAGE_SAFETY_CAP} pages reached, stopping")
                break

            elapsed = time.time() - start_time
            if elapsed > self._CRAWL_BUDGET_SECS:
                print(f"[epa-ie-news-releases] wall-clock budget exceeded ({elapsed:.0f}s), stopping cleanly")
                break

            page_num += 1

            # Re-use the already-fetched HTML for the start page
            if year_url == self._START_URL and start_html:
                year_html = start_html
                start_html = None  # only reuse once
            else:
                time.sleep(self._delay)
                year_html = self._curl_get(year_url)
                if not year_html:
                    print(f"[epa-ie-news-releases] failed to fetch {year_url}, skipping")
                    continue

            if page_num % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[epa-ie-news-releases] page {page_num}: saved {saved}/{lim_str}")

            items = self._parse_list_page(year_html)
            if not items:
                print(f"[epa-ie-news-releases] no items on {year_url}, skipping")
                continue
            print(f"[epa-ie-news-releases] {year_url}: found {len(items)} items")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                elapsed = time.time() - start_time
                if elapsed > self._CRAWL_BUDGET_SECS:
                    print(f"[epa-ie-news-releases] budget exceeded mid-page, stopping")
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    print(f"[epa-ie-news-releases] duplicate URL skipped: {item_url}")
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl_get(item_url)
                    if not detail_html:
                        print(f"[epa-ie-news-releases] detail fetch failed: {item_url}, skipping")
                        continue

                    detail = self._parse_detail_page(detail_html)

                    title = detail["title"] or item["title"]
                    abstract = detail["abstract"] or item["abstract_short"]
                    published_date = detail["published_date"] or item["listed_date"]

                    if not abstract or len(abstract) < 50:
                        print(
                            f"[epa-ie-news-releases] abstract too short "
                            f"({len(abstract) if abstract else 0} chars), skipping: {title[:60]}"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": item["external_id"],
                        "post_number": item["post_number"],
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": item["listed_date"],
                        "url": item_url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "publisher": self._PUBLISHER,
                        "authors": None,
                        "keywords": None,
                        "category": "News Release",
                        "doi": None,
                        "department": None,
                        "journal": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": item["listed_date"],
                                "year_page": year_url,
                                "node_id": item["external_id"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[epa-ie-news-releases] saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[epa-ie-news-releases] item {item.get('external_id', '?')} failed: {exc}")
                    continue

        print(f"[epa-ie-news-releases] done. total saved: {saved}")
        return saved
