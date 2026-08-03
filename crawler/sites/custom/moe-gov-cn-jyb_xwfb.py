# -*- coding: utf-8 -*-
"""中华人民共和国教育部政府门户网站 - 政策解读 crawler.

List pages:
  Page 1 : http://www.moe.gov.cn/jyb_xwfb/s271/
  Pages 2-25 : .../index_{page-1}.html   (static HTML)
  Pages 26+  : /was5/web/search?channelid=254874&chnlid=271&page=N  (HTML fragment)
  Total: ~573 records, 20/page → ~29 pages

Detail pages: .../YYYYMM/tYYYYMMDD_{ID}.html
  - <meta name="contentid">   → external_id / post_number
  - <meta name="publishdate"> → published_date
  - <meta name="author">      → authors
  - <meta name="source">      → publisher
  - <div class=TRS_Editor>    → abstract (body text)
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_LIST_BASE = "http://www.moe.gov.cn/jyb_xwfb/s271/"
_WAS_API_BASE = "http://www.moe.gov.cn/was5/web/search"
_WAS_CHANNEL = "254874"
_WAS_CHNL = "271"
_BACKOFFS = [1, 3, 9]


def _decode(raw):
    """Decode bytes with UTF-8 → GB18030 → replace fallback."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    for enc in ("utf-8", "gb18030", "gbk"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, AttributeError):
            pass
    return raw.decode("utf-8", errors="replace")


def _make_soup(html):
    """BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
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


class MoeGovCnJybXwfbCrawler(BaseCrawler):
    """Crawler for 中华人民共和国教育部 - 政策解读 (s271)."""

    site_id = "moe-gov-cn-jyb_xwfb"
    site_name = "Custom: moe-gov-cn-jyb_xwfb"
    base_url = "http://www.moe.gov.cn"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_raw(self, url):
        """GET via curl with 3 attempts and exponential backoff. Returns str or None."""
        for attempt in range(3):
            try:
                cmd = [
                    "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
                    "-H", f"User-Agent: {self.USER_AGENT}",
                    url,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.stdout and result.stdout.strip():
                    return _decode(result.stdout)
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3) {url}: {exc}")
            if attempt < 2:
                time.sleep(_BACKOFFS[attempt])
        print(f"[{self.site_id}] Failed after 3 attempts: {url}")
        return None

    def _parse_list_items(self, html, base_url):
        """Parse list HTML (full page or WAS fragment). Returns [(url, title, date_str)]."""
        soup = _make_soup(html)
        if soup is None:
            return []
        items = []
        # Full page has <ul id="list">; WAS fragment is bare <li> elements.
        ul = soup.find("ul", id="list")
        li_tags = ul.find_all("li") if ul else soup.find_all("li")
        for li in li_tags:
            a = li.find("a")
            span = li.find("span")
            if not a:
                continue
            href = (a.get("href") or "").strip()
            title = (a.get("title") or a.get_text(strip=True)).strip()
            date_str = span.get_text(strip=True) if span else ""
            if not href or not title:
                continue
            url = href if href.startswith("http") else urljoin(base_url, href)
            items.append((url, title, date_str))
        return items

    def _parse_detail(self, url):
        """Fetch and parse detail page. Returns dict or None on failure."""
        html = self._fetch_raw(url)
        if not html:
            return None
        soup = _make_soup(html)
        if soup is None:
            return None

        def _meta(name):
            tag = soup.find("meta", attrs={"name": name})
            return (tag.get("content") or "").strip() if tag else ""

        external_id = _meta("contentid")
        published_date = _meta("publishdate")
        # Strip time component if present ("2026-04-10 11:26" → "2026-04-10")
        if published_date and " " in published_date:
            published_date = published_date.split()[0]
        author = _meta("author")
        publisher = _meta("source") or _meta("ContentSource")

        # Article body: <div class=TRS_Editor> (attr without quotes → html5lib handles it)
        content_div = soup.find("div", class_="TRS_Editor")
        abstract = ""
        if content_div:
            parts = []
            for elem in content_div.find_all(["p", "li", "td"]):
                text = elem.get_text(separator=" ", strip=True)
                if len(text) > 5:
                    parts.append(text)
            abstract = "\n".join(parts) if parts else content_div.get_text(separator="\n", strip=True)

        # listed_date and publisher from .moe-detail-shuxing ("2026-04-10　来源：教育部")
        listed_date = published_date
        for shuxing in soup.find_all("div", class_="moe-detail-shuxing"):
            text = shuxing.get_text(strip=True)
            m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
            if m:
                listed_date = m.group(1)
            if not publisher:
                src_m = re.search(r'来源[：:]([^（\n\r]+)', text)
                if src_m:
                    publisher = src_m.group(1).strip()
            break  # only need first shuxing div

        return {
            "external_id": external_id,
            "published_date": published_date,
            "listed_date": listed_date,
            "author": author,
            "publisher": publisher or "教育部",
            "abstract": abstract,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 政策解读 list, fetch detail pages, save documents.

        Pagination:
          - Page 1        : static main URL
          - Pages 2-25    : index_{page-1}.html (static)
          - Pages 26+     : WAS AJAX API
        Terminates when: limit reached, empty page, all-seen page, or safety caps.
        """
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        while True:
            # Wall-clock budget
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}. Stopping.")
                break
            # Safety page cap
            if page > MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached.")
                break
            # Limit reached before fetching next page
            if limit is not None and saved >= limit:
                break

            # Build list URL for this page
            if page == 1:
                list_url = _LIST_BASE
            elif page <= 25:
                list_url = f"{_LIST_BASE}index_{page - 1}.html"
            else:
                list_url = (
                    f"{_WAS_API_BASE}?channelid={_WAS_CHANNEL}&chnlid={_WAS_CHNL}&page={page}"
                )

            # Progress log every 10 pages
            if page == 1 or page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            html = self._fetch_raw(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            all_items = self._parse_list_items(html, _LIST_BASE)
            if not all_items:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            # Deduplicate across pages
            new_items = []
            for item in all_items:
                url = item[0]
                if url not in seen_urls:
                    seen_urls.add(url)
                    new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            for url, title, date_str in new_items:
                if limit is not None and saved >= limit:
                    break

                try:
                    time.sleep(self._delay)

                    # post_number: numeric ID from URL (e.g. "1433232")
                    m = re.search(r't\d{8}_(\d+)\.html', url)
                    post_number = m.group(1) if m else None

                    detail = self._parse_detail(url)
                    if not detail:
                        print(f"[{self.site_id}] item {url} failed: detail fetch returned None")
                        continue

                    abstract = detail.get("abstract", "").strip()
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract <50 chars for {url}. Skipping.")
                        continue

                    external_id = detail.get("external_id") or post_number or url
                    published_date = detail.get("published_date") or date_str
                    listed_date = detail.get("listed_date") or date_str
                    author = detail.get("author", "")
                    publisher = detail.get("publisher") or "教育部"

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": url,
                        "authors": author,
                        "publisher": publisher,
                        "pdf_url": None,
                        "keywords": None,
                        "category": "政策解读",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {"posted_date": date_str, "contentid": external_id},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {url} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
