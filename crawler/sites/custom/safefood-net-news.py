# -*- coding: utf-8 -*-
"""Crawler for safefood.net press releases.

Starting URL: https://www.safefood.net/news
  → redirects to: https://www.safefood.net/communications/news

The site organises press releases by year (2023–current) at:
  /communications/news/{year}
Each year page is a single HTML listing with title, summary, date, and URL.
Detail pages contain the full body text in div.padding-article.
No numeric post IDs — the URL slug is used as the native identifier.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # noqa: E402  absolute import


_MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date(raw: str) -> str | None:
    """Parse "DD Month YYYY" or ISO dates → "YYYY-MM-DD". Returns None on failure."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%d %B %Y", "%d %b %Y", "%B %d, %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Manual fallback for mixed-case month names
    m = re.match(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", raw)
    if m:
        day, month_str, year = m.groups()
        month_num = _MONTH_MAP.get(month_str.lower())
        if month_num:
            try:
                return f"{int(year):04d}-{month_num:02d}-{int(day):02d}"
            except ValueError:
                pass
    return None


class SafefoodNetNewsCrawler(BaseCrawler):
    site_id = "safefood-net-news"
    site_name = "Custom: safefood-net-news"
    base_url = "https://www.safefood.net"

    _NEWS_ROOT = "https://www.safefood.net/communications/news"
    _PUBLISHER = "Safefood"
    _PAGE_SAFETY_CAP = 200
    _CRAWL_BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _RATE_SLEEP = 1.0

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential backoff. Returns decoded text or None."""
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL",
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
                print(f"[{self.site_id}] empty response attempt {attempt + 1}/3 for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt + 1}/3: {exc}")
            if attempt < 2:
                time.sleep(waits[attempt])
        print(f"[{self.site_id}] all 3 attempts failed for {url}")
        return None

    def _make_soup(self, html: str):
        """Parse HTML with html5lib → lxml → html.parser fallback."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # Discovery helpers
    # ------------------------------------------------------------------

    def _discover_years(self) -> list[int]:
        """Fetch the news root and extract year numbers from sidebar navigation."""
        html = self._curl_get(self._NEWS_ROOT)
        if not html:
            return []
        soup = self._make_soup(html)
        if not soup:
            return []
        years: set[int] = set()
        pattern = re.compile(r"^/communications/news/(\d{4})$")
        for a in soup.find_all("a", href=True):
            m = pattern.match(a["href"])
            if m:
                years.add(int(m.group(1)))
        return sorted(years)

    def _get_year_articles(self, year: int) -> list[dict]:
        """Fetch a year listing page and return article dicts (title/summary/date/url/slug)."""
        year_url = f"{self._NEWS_ROOT}/{year}"
        html = self._curl_get(year_url)
        if not html:
            print(f"[{self.site_id}] failed to fetch year page: {year_url}")
            return []
        soup = self._make_soup(html)
        if not soup:
            return []

        articles: list[dict] = []
        for li in soup.select("li"):
            title_p = li.find("p", class_="title")
            summary_p = li.find("p", class_="summary")
            meta_p = li.find("p", class_="meta")
            if not (title_p and meta_p):
                continue
            link = title_p.find("a")
            if not link:
                continue
            href = link.get("href", "")
            # Must be a news detail link (>= 4 path segments)
            if not href.startswith("/communications/news/") or href.count("/") < 3:
                continue
            full_url = f"{self.base_url}{href}"
            date_raw = meta_p.get_text(separator=" ", strip=True)
            # Strip icon text (e.g. "szicon-calendar" font characters)
            date_raw = re.sub(r"[^\w\s]", " ", date_raw).strip()
            date_raw = re.sub(r"\s+", " ", date_raw)
            articles.append({
                "title": title_p.get_text(strip=True),
                "summary": summary_p.get_text(strip=True) if summary_p else "",
                "date_raw": date_raw,
                "url": full_url,
                "slug": href.lstrip("/"),  # e.g. "communications/news/2026/kitchen-hygiene-confidence"
            })
        return articles

    # ------------------------------------------------------------------
    # Detail page extraction
    # ------------------------------------------------------------------

    def _fetch_detail(self, art: dict) -> dict | None:
        """Fetch article detail page and return enriched dict, or None to skip."""
        url = art["url"]
        slug = art["slug"]
        # post_number = terminal slug segment (e.g. "kitchen-hygiene-confidence")
        post_number = slug.split("/")[-1] if "/" in slug else slug

        html = self._curl_get(url)
        if not html:
            print(f"[{self.site_id}] failed to fetch detail: {url}")
            return None

        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] soup parse error for {url}: {exc}")
            return None
        if not soup:
            return None

        # Title: prefer the page's <h1>
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else art["title"]
        if not title:
            title = art["title"]

        # Date: from the detail page's meta paragraph (more reliable than listing)
        date_str = None
        content_div = soup.select_one("div.padding-article")
        if content_div:
            meta_p = content_div.find("p", class_="meta")
            if meta_p:
                raw = meta_p.get_text(separator=" ", strip=True)
                raw = re.sub(r"[^\w\s]", " ", raw).strip()
                raw = re.sub(r"\s+", " ", raw)
                date_str = _parse_date(raw)
        if not date_str:
            date_str = _parse_date(art.get("date_raw", ""))

        # Abstract: full body text from div.padding-article
        abstract = ""
        if content_div:
            # Deep-copy the div to avoid mutating the original tree
            import copy
            body = copy.copy(content_div)
            # Remove noise elements
            for tag in body.find_all(["nav", "script", "style", "form", "button"]):
                tag.decompose()
            for p in body.find_all("p", class_="meta"):
                p.decompose()
            for p in body.find_all("p", class_="title"):
                p.decompose()
            # Remove "Print" link
            for a in body.find_all("a"):
                if re.match(r"^\s*print\s*$", a.get_text(), re.I):
                    a.decompose()
            abstract = body.get_text(separator=" ", strip=True)
            abstract = re.sub(r"\s+", " ", abstract).strip()

        if len(abstract) < 50:
            print(f"[{self.site_id}] abstract too short for {url}, skipping.")
            return None

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "url": url,
            "title": title,
            "abstract": abstract,
            "published_date": date_str,
            "posted_date": date_str,
            "publisher": self._PUBLISHER,
            "pdf_url": None,
            "metadata": json.dumps({
                "slug": slug,
                "post_number": post_number,
                "listing_summary": art.get("summary", ""),
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl safefood.net press releases, newest-first by year.

        Parameters
        ----------
        limit : int or None
            Maximum number of items to save. None = unlimited.
        """
        start_time = time.time()
        limit_display = limit if limit is not None else "∞"
        seen_urls: set[str] = set()
        saved = 0
        page_count = 0

        # 1. Discover year pages from the root nav
        years = self._discover_years()
        if not years:
            print(f"[{self.site_id}] No year pages discovered; aborting.")
            return 0
        print(f"[{self.site_id}] Found years: {sorted(years, reverse=True)}")

        done = False
        for year in sorted(years, reverse=True):
            if done:
                break
            if time.time() - start_time > self._CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] Wall-clock budget exceeded, stopping.")
                break
            if page_count >= self._PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {self._PAGE_SAFETY_CAP} pages reached, stopping.")
                break

            articles = self._get_year_articles(year)
            page_count += 1
            if page_count % 10 == 0 or page_count == 1:
                print(f"[{self.site_id}] page {page_count}: saved {saved}/{limit_display}")

            for art in articles:
                if done:
                    break
                if time.time() - start_time > self._CRAWL_BUDGET_SECS:
                    print(f"[{self.site_id}] Wall-clock budget exceeded mid-year, stopping.")
                    done = True
                    break

                url = art["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    detail = self._fetch_detail(art)
                    if detail is None:
                        continue
                    self._save_paper(detail)
                    saved += 1
                    if limit is not None and saved >= limit:
                        done = True
                        break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

                time.sleep(self._RATE_SLEEP)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
