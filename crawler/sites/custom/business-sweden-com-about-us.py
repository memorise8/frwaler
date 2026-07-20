# -*- coding: utf-8 -*-
"""Business Sweden - Press Releases crawler.

Target: https://www.business-sweden.com/about-us/media/press-releases/
Pagination: ?page=N  (6 items per page)
Detail page: /about-us/media/press-releases/press-releases/YYYY/slug/
"""

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.business-sweden.com"
_LIST_URL = "https://www.business-sweden.com/about-us/media/press-releases/"

_MONTH_MAP = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _parse_month_day_year(text: str) -> str:
    """Parse 'March 25, 2026' or 'Mar 25, 2026' → 'YYYY-MM-DD'."""
    m = re.search(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", text.strip())
    if m:
        mo = _MONTH_MAP.get(m.group(1).lower()[:3], "")
        if mo:
            return f"{m.group(3)}-{mo}-{m.group(2).zfill(2)}"
    return ""


def _parse_dot_date(text: str) -> str:
    """Parse 'DD.MM.YYYY' → 'YYYY-MM-DD'."""
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return ""


def _bs(html: str):
    """BeautifulSoup with parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _text(tag) -> str:
    if tag is None:
        return ""
    return re.sub(r"\s+", " ", tag.get_text(separator=" ", strip=True))


class BusinessSwedenPressReleasesCrawler(BaseCrawler):
    site_id = "business-sweden-com-about-us"
    site_name = "Custom: business-sweden-com-about-us"
    base_url = "https://www.business-sweden.com"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """HTTP GET via curl with 3-attempt exponential backoff."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = 3 ** attempt  # 1s, 3s
                    print(f"[{self.site_id}] Empty response for {url}, retry in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[{self.site_id}] curl error ({url}): {exc}, retry in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts ({url}): {exc}")
        return None

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str):
        """Return (items, has_next).

        items is a list of dicts with keys: url, title, listed_date, snippet.
        has_next is True if a non-disabled Next button is present.
        """
        items = []
        try:
            soup = _bs(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error on list page: {exc}")
            return items, False
        if soup is None:
            return items, False

        pagination_div = soup.find("div", class_="js-pagination")
        if pagination_div is None:
            return items, False

        for item_div in pagination_div.find_all("div", class_="CalendarList-item"):
            # --- Date from CalendarList-itemTime ("March 25, 2026") ---
            time_span = item_div.find("span", class_="CalendarList-itemTime")
            listed_date = _parse_month_day_year(_text(time_span)) if time_span else ""

            # --- Title + URL ---
            h6 = item_div.find("h6", class_=lambda c: c and "Heading--h8" in c)
            link = h6.find("a") if h6 else None
            if not link:
                continue
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if not href:
                continue
            url = href if href.startswith("http") else _BASE + href

            # --- Short snippet from list ---
            intro_div = item_div.find("div", class_="CalendarList-itemIntro")
            snippet = ""
            if intro_div:
                p = intro_div.find("p")
                if p:
                    snippet = p.get_text(strip=True)

            items.append({
                "url": url,
                "title": title,
                "listed_date": listed_date,
                "snippet": snippet,
            })

        # --- Pagination: is there a non-disabled Next button? ---
        has_next = False
        for li in soup.find_all("li", class_=lambda c: c and "Pagination-item--nav" in c and "next" in c):
            classes = li.get("class") or []
            if "is-disabled" not in classes:
                has_next = True
                break

        return items, has_next

    # ------------------------------------------------------------------
    # Detail-page fetcher
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str) -> dict:
        """Fetch and parse a detail page.

        Returns dict with keys: abstract, published_date, page_id.
        """
        html = self._curl_get(url)
        if not html:
            return {}

        result = {}

        # --- pageid from <meta name="pageid" content="..."> ---
        m = re.search(r'<meta\s[^>]*name=["\']pageid["\'][^>]*content=["\'](\d+)["\']', html)
        if not m:
            m = re.search(r'<meta\s[^>]*content=["\'](\d+)["\'][^>]*name=["\']pageid["\']', html)
        if m:
            result["page_id"] = m.group(1)

        try:
            soup = _bs(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error on detail {url}: {exc}")
            return result
        if soup is None:
            return result

        # --- Published date from ArticleHead-intro (contains DD.MM.YYYY) ---
        intro_div = soup.find(class_="ArticleHead-intro")
        if intro_div:
            intro_text = _text(intro_div)
            published_date = _parse_dot_date(intro_text)
            if published_date:
                result["published_date"] = published_date

        # --- Lead paragraph from ArticleHead-banner ---
        banner_div = soup.find(class_="ArticleHead-banner")
        banner_text = _text(banner_div) if banner_div else ""

        # --- Full body from Article-body ---
        body_div = soup.find(class_="Article-body")
        body_text = _text(body_div) if body_div else ""

        # Build abstract: lead paragraph + body (both if available)
        parts = []
        if len(banner_text) > 20:
            parts.append(banner_text)
        if len(body_text) > 20:
            parts.append(body_text)
        if not parts and intro_div:
            # Fallback: strip known prefixes from intro text
            raw = _text(intro_div)
            raw = re.sub(r"^Press release\s+", "", raw, flags=re.IGNORECASE)
            if len(raw) > 20:
                parts.append(raw)

        result["abstract"] = "\n\n".join(parts)
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_or_inf = limit if limit is not None else "∞"
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_PAGES = 200
        WALL_SECS = 25 * 60  # 25 minutes

        page = 1
        while True:
            # --- Global stop conditions ---
            if limit is not None and saved >= limit:
                break
            if page > MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {MAX_PAGES} pages, stopping.")
                break
            if time.time() - start_time > WALL_SECS:
                print(f"[{self.site_id}] Approaching 25-minute wall-clock limit, stopping.")
                break

            # --- Fetch list page ---
            list_url = f"{_LIST_URL}?page={page}"
            html = self._curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping.")
                break

            items, has_next = self._parse_list_page(html)

            if not items:
                print(f"[{self.site_id}] No items on page {page}, done.")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            # --- Process each item ---
            for item in items:
                if limit is not None and saved >= limit:
                    break

                item_url = item["url"]
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(item_url)

                    abstract = detail.get("abstract", "").strip()
                    # Fallback to list snippet if detail failed
                    if len(abstract) < 50:
                        abstract = item.get("snippet", "").strip()
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping short abstract (<50 chars): {item_url}")
                        continue

                    published_date = detail.get("published_date", item.get("listed_date", ""))
                    listed_date = item.get("listed_date", published_date)
                    page_id = detail.get("page_id", "")

                    # post_number: numeric pageid preferred, else slug
                    slug = [s for s in item_url.rstrip("/").split("/") if s][-1]
                    post_number = page_id if page_id else slug
                    external_id = post_number

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": item_url,
                        "pdf_url": None,
                        "authors": "",
                        "publisher": "Business Sweden",
                        "department": "",
                        "journal": "",
                        "keywords": "",
                        "category": "Press Release",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "page_id": page_id,
                            "slug": slug,
                            "snippet": item.get("snippet", ""),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] Saved {counter}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            if not has_next:
                print(f"[{self.site_id}] No next page after page {page}, done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
