# -*- coding: utf-8 -*-
"""Crawler for Chinese Academy of Fiscal Sciences (CAFS) - Research Reports.

Target: https://www.chineseafs.org/ckynewsmgr/newsContentQueryByPage.action
        ?searchKey=enyjbg&queryFlag=1&address=Research+Reports&retVal=enyjbggdxw
        &start=0&limit=10

Uses curl with --tls-max 1.3 to bypass SSL compatibility issues.
Paginated via ?start=N offset; detail page fetched per item.
"""

import json
import re
import subprocess
import time
import uuid

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.chineseafs.org"
_LIST_URL = (
    _BASE
    + "/ckynewsmgr/newsContentQueryByPage.action"
    "?searchKey=enyjbg&queryFlag=1&address=Research+Reports&retVal=enyjbggdxw"
)
_DETAIL_BASE = _BASE + "/ckynewsmgr/newsContent_queryOneNewsRecord"
_PAGE_SIZE = 10  # match site default to avoid unexpected behaviour
_SITE_ID = "chineseafs-org-ckynewsmgr"


# ---------------------------------------------------------------------------
# Low-level helpers (module-level so they can be used without an instance)
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch *url* via curl with TLS workaround; returns decoded text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                return raw.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1s, 3s, 9s
                print(f"[{_SITE_ID}] Empty response attempt {attempt+1}/{retries}; "
                      f"retrying in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error attempt {attempt+1}/{retries}: {exc}; "
                      f"retrying in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _make_soup(html: str):
    """Construct BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html_fragment: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_fragment)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class ChineseAFSCrawler(BaseCrawler):
    """Crawler for CAFS (Chinese Academy of Fiscal Sciences) Research Reports."""

    site_id = _SITE_ID
    site_name = "Custom: chineseafs-org-ckynewsmgr"
    base_url = _BASE

    # ------------------------------------------------------------------
    # Internal fetch helpers
    # ------------------------------------------------------------------

    def _fetch_list(self, start: int) -> str | None:
        url = f"{_LIST_URL}&start={start}&limit={_PAGE_SIZE}"
        return _curl_get(url)

    def _fetch_detail(self, newsid: str) -> str | None:
        url = f"{_DETAIL_BASE}?retVal=enyjxmxq&zyflag=2&searchFlag=2&newsid={newsid}"
        return _curl_get(url)

    # ------------------------------------------------------------------
    # Parse helpers
    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> tuple[list[dict], int]:
        """Return ``(items, total_count)`` from a list page HTML string."""
        # Total record count is embedded in a JS variable: var totalrecord = '30';
        total = 0
        m = re.search(r"var\s+totalrecord\s*=\s*'(\d+)'", html)
        if m:
            total = int(m.group(1))

        items: list[dict] = []
        seen: set[str] = set()
        # Only news-detail links carry newsid=…
        for m in re.finditer(
            r'href="(/ckynewsmgr/newsContent_queryOneNewsRecord[^"]*newsid=([^"&\s]+))"',
            html,
        ):
            newsid = m.group(2)
            if newsid in seen:
                continue
            seen.add(newsid)
            items.append({
                "newsid": newsid,
                "url": _BASE + m.group(1),
            })
        return items, total

    def _parse_detail(self, html: str) -> dict | None:
        """Parse a detail page; return a dict or None if unusable."""
        try:
            soup = _make_soup(html)
        except Exception:
            soup = None

        # --- Title ---
        title = ""
        if soup:
            try:
                h2 = soup.find("h2")
                if h2:
                    title = h2.get_text(separator=" ", strip=True)
            except Exception:
                pass
        if not title:
            m = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL)
            if m:
                title = _strip_tags(m.group(1))
        if not title:
            return None

        # --- Published date ---
        published_date = ""
        if soup:
            try:
                date_tag = soup.find("span", class_="fabueiqi")
                if date_tag:
                    dm = re.search(r"(\d{4}-\d{2}-\d{2})", date_tag.get_text())
                    if dm:
                        published_date = dm.group(1)
            except Exception:
                pass
        if not published_date:
            dm = re.search(r"Release date[^:]*:\s*(\d{4}-\d{2}-\d{2})", html)
            if dm:
                published_date = dm.group(1)

        # --- Abstract / content ---
        abstract = ""
        authors: list[str] = []

        if soup:
            try:
                content_div = soup.find("div", class_="news-texe")
                if content_div:
                    # Try to extract author name: centered short paragraphs near top,
                    # not containing digits or typical heading words.
                    for p in content_div.find_all("p")[:8]:
                        style = p.get("style", "")
                        text = p.get_text(strip=True)
                        if "center" in style and 2 < len(text) < 80:
                            if not re.search(
                                r"\d{4}|No\.|Abstract|January|February|March|"
                                r"April|May|June|July|August|September|October|"
                                r"November|December|Public Finance",
                                text, re.IGNORECASE,
                            ):
                                authors.append(text)
                                break

                    full_text = content_div.get_text(separator=" ", strip=True)
                    # Prefer the content after the "Abstract" heading
                    abs_m = re.search(
                        r"\bAbstract\b\s+(.*)",
                        full_text,
                        re.DOTALL | re.IGNORECASE,
                    )
                    abstract = abs_m.group(1).strip() if abs_m else full_text
            except Exception:
                pass

        if not abstract:
            # Regex fallback when BeautifulSoup failed entirely
            m = re.search(r'class="news-texe"[^>]*>(.*?)</div>', html, re.DOTALL)
            if m:
                abstract = _strip_tags(m.group(1))

        if len(abstract) < 50:
            return None

        return {
            "title": title,
            "published_date": published_date,
            "abstract": abstract,
            "authors": authors,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60   # 25 minutes
        SAFETY_CAP = 200             # max list pages
        limit_str = str(limit) if limit is not None else "∞"

        p = 0          # page counter (0-based)
        start = 0      # list-page offset
        total_known = 0

        while True:
            # ---- stop conditions ----
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; exiting cleanly.")
                break
            if p >= SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {SAFETY_CAP} pages reached; exiting.")
                break

            # ---- fetch list page ----
            raw = self._fetch_list(start)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page (start={start}). Stopping.")
                break

            items, total_count = self._parse_list(raw)

            if p == 0 and total_count:
                total_known = total_count
                print(f"[{self.site_id}] Total records reported by server: {total_known}")

            if not items:
                print(f"[{self.site_id}] No items at start={start}. Done.")
                break

            # URL-dedup: skip pages where every URL was already seen
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {p+1} already seen. Done.")
                break

            # ---- process each item ----
            for it in new_items:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > MAX_WALL_SECONDS:
                    break

                seen_urls.add(it["url"])

                try:
                    time.sleep(self._delay)
                    raw_detail = self._fetch_detail(it["newsid"])
                    if not raw_detail:
                        print(f"[{self.site_id}] Item {it['newsid']}: fetch failed; skipping.")
                        continue

                    parsed = self._parse_detail(raw_detail)
                    if not parsed:
                        print(f"[{self.site_id}] Item {it['newsid']}: abstract <50 chars "
                              "or no title; skipping.")
                        continue

                    paper = {
                        "id": str(uuid.uuid4()),
                        "site_id": self.site_id,
                        "external_id": it["newsid"],
                        "title": parsed["title"],
                        "authors": json.dumps(parsed["authors"], ensure_ascii=False),
                        "abstract": parsed["abstract"],
                        "category": "Research Reports",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": it["url"],
                        "pdf_url": "",
                        "doi": "",
                        "department": "",
                        "metadata": json.dumps({}, ensure_ascii=False),
                    }
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: "
                          f"{parsed['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] Item {it['newsid']} failed: {exc}; continuing.")
                    continue

            # Progress log every 10 pages
            if p > 0 and p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_str}")

            # Advance pagination
            p += 1
            start += _PAGE_SIZE

            # Stop when we've walked past all known records
            if total_known > 0 and start >= total_known:
                print(f"[{self.site_id}] Reached end of records "
                      f"(start={start} >= total={total_known}). Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
