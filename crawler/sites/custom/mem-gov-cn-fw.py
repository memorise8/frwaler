# -*- coding: utf-8 -*-
"""Crawler for 应急管理部行政许可事项办理结果与办事统计 (mem.gov.cn).

Target: https://www.mem.gov.cn/fw/yajzjxzxk/zjxzxksxbljg/
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_LIST_BASE = "https://www.mem.gov.cn/fw/yajzjxzxk/zjxzxksxbljg/"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl_get(url, timeout=30, retries=3):
    """GET via curl with gzip decompression.  Returns decoded text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--compressed",
        "--max-time", str(timeout),
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("gbk", errors="replace")
            # Empty response — back off and retry
            wait = 3 ** attempt  # 1, 3, 9
            if attempt < retries - 1:
                print(f"[mem-gov-cn-fw] Empty response (attempt {attempt+1}/{retries}), "
                      f"retry in {wait}s: {url}")
                time.sleep(wait)
        except Exception as exc:
            wait = 3 ** attempt
            if attempt < retries - 1:
                print(f"[mem-gov-cn-fw] curl error (attempt {attempt+1}/{retries}): {exc}, "
                      f"retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[mem-gov-cn-fw] curl failed after {retries} attempts: {exc}")
    return None


def _strip_tags(html):
    """Strip HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", html)
    for entity, char in [
        ("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"),
        ("&amp;", "&"), ("&quot;", '"'), ("&#39;", "'"),
    ]:
        text = text.replace(entity, char)
    text = re.sub(r"&[a-zA-Z#\d]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(html):
    """Try parser chain (html5lib → lxml → html.parser).  Returns soup or None."""
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


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MemGovCnFwCrawler(BaseCrawler):
    """Ministry of Emergency Management — administrative licence results."""

    site_id = "mem-gov-cn-fw"
    site_name = "Custom: mem-gov-cn-fw"
    base_url = "https://www.mem.gov.cn"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_url(self, page_idx):
        if page_idx == 0:
            return _LIST_BASE
        return f"{_LIST_BASE}index_{page_idx}.shtml"

    def _parse_list(self, html):
        """Return [(rel_href, title, date_str), ...] from a list page."""
        results = []
        pat = re.compile(
            r'<a\s+href="(\./\d{6}/t[\w]+\.shtml)">'
            r'(.*?)<span>([\d\-\s:]+)</span>\s*</a>',
            re.DOTALL,
        )
        for m in pat.finditer(html):
            href = m.group(1)
            title = _strip_tags(m.group(2)).strip()
            date_str = m.group(3).strip()
            if title:
                results.append((href, title, date_str))
        return results

    def _page_count(self, html):
        m = re.search(r"var\s+countPage\s*=\s*(\d+)", html)
        return int(m.group(1)) if m else 1

    def _fetch_detail(self, url):
        """Fetch and parse a detail page.

        Returns (title, abstract, pub_date, ext_id, post_num) or None on failure.
        """
        html = _curl_get(url)
        if not html:
            return None

        # Title: <h2> in .zhenwen div
        title = ""
        h2 = re.search(r"<h2[^>]*>(.*?)</h2>", html, re.DOTALL | re.IGNORECASE)
        if h2:
            title = _strip_tags(h2.group(1)).strip()

        # Publication date: first date-like span in .time_laiy
        pub_date = ""
        dm = re.search(
            r'class="time_laiy"[^>]*>.*?<span>([\d]{4}-[\d]{2}-[\d]{2})',
            html, re.DOTALL,
        )
        if dm:
            pub_date = dm.group(1).strip()

        # Abstract: text content of TRS_Editor div
        abstract = ""
        try:
            soup = _make_soup(html)
            if soup:
                editor = soup.find("div", class_="TRS_Editor")
                if editor:
                    abstract = re.sub(
                        r"\s+", " ", editor.get_text(separator=" ")
                    ).strip()
        except Exception as exc:
            print(f"[mem-gov-cn-fw] soup parse error for {url}: {exc}")

        # Regex fallback for abstract
        if not abstract:
            trs = re.search(
                r"<div\s[^>]*class=TRS_Editor[^>]*>(.*)",
                html, re.DOTALL | re.IGNORECASE,
            )
            if trs:
                abstract = _strip_tags(trs.group(1)[:80000]).strip()

        # External id and post_number from URL
        ext_id = None
        post_num = None
        m2 = re.search(r"(t\d+_(\d+))\.shtml", url)
        if m2:
            ext_id = m2.group(1)
            post_num = m2.group(2)

        return title, abstract, pub_date, ext_id, post_num

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        wall_start = time.monotonic()
        seen_urls = set()
        saved = 0

        # --- Fetch first page (also gives total page count) ---
        first_html = _curl_get(self._list_url(0))
        if not first_html:
            print("[mem-gov-cn-fw] Failed to fetch first list page. Aborting.")
            return 0

        total_pages = self._page_count(first_html)
        print(f"[mem-gov-cn-fw] Total pages: {total_pages}")

        for page_idx in range(total_pages):
            # --- Limit / budget guards ---
            if limit is not None and saved >= limit:
                break
            if page_idx >= _MAX_PAGES:
                print(f"[mem-gov-cn-fw] Safety cap ({_MAX_PAGES} pages) reached. Logging and stopping.")
                break
            if time.monotonic() - wall_start > _WALL_BUDGET:
                print("[mem-gov-cn-fw] Wall-clock budget exceeded (25 min). Stopping cleanly.")
                break

            # Progress log every 10 pages
            if page_idx % 10 == 0:
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[mem-gov-cn-fw] page {page_idx}: saved {saved}/{lim_str}")

            # --- Fetch list page ---
            if page_idx == 0:
                html = first_html
            else:
                time.sleep(self._delay)
                html = _curl_get(self._list_url(page_idx))
                if not html:
                    print(f"[mem-gov-cn-fw] Failed to fetch list page {page_idx}, skipping.")
                    continue

            items = self._parse_list(html)
            if not items:
                print(f"[mem-gov-cn-fw] No items on page {page_idx}. Done.")
                break

            new_on_page = 0
            for href, list_title, list_date in items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - wall_start > _WALL_BUDGET:
                    print("[mem-gov-cn-fw] Wall-clock budget exceeded. Stopping.")
                    break

                # Build absolute URL: ./202604/t...shtml → base + 202604/t...
                rel = href[2:] if href.startswith("./") else href.lstrip("/")
                detail_url = _LIST_BASE + rel

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    result = self._fetch_detail(detail_url)
                    if result is None:
                        print(f"[mem-gov-cn-fw] item {detail_url} failed: no response")
                        continue

                    title, abstract, pub_date, ext_id, post_num = result

                    title = title or list_title
                    if not pub_date:
                        pub_date = list_date[:10] if list_date else ""

                    if not abstract or len(abstract) < 50:
                        print(
                            f"[mem-gov-cn-fw] Skipping short abstract "
                            f"({len(abstract)} chars): {title[:50]}"
                        )
                        continue

                    listed_date = list_date[:10] if list_date else None

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": ext_id or detail_url.split("/")[-1],
                        "post_number": post_num,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "posted_date": listed_date,
                        "url": detail_url,
                        "pdf_url": None,
                        "authors": None,
                        "publisher": "中华人民共和国应急管理部",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "doi": None,
                        "category": "行政许可",
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": list_date,
                                "category": "行政许可",
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[mem-gov-cn-fw] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[mem-gov-cn-fw] item {detail_url} failed: {exc}")
                    continue

            # If zero new URLs seen on this page, the paginator has looped — stop.
            if new_on_page == 0 and page_idx > 0:
                print(f"[mem-gov-cn-fw] No new URLs on page {page_idx} (dedup loop detected). Done.")
                break

        print(f"[mem-gov-cn-fw] Done. Total saved: {saved}")
        return saved
