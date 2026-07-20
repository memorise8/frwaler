# -*- coding: utf-8 -*-
"""NHC 国家卫生健康委员会 规范性文件 (Normative Documents) crawler.

Starting URL: https://www.nhc.gov.cn/wjw/gfxwjj/list.shtml
Bot protection: WZWS (Wangsu/网宿) WAF — requires Playwright to solve JS challenge.
Pagination: list.shtml (page 1), list_2.shtml, list_3.shtml, ... (63 pages, ~1500 items)
"""

from __future__ import annotations

import json
import re
import sys
import time

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.nhc.gov.cn"
_LIST_URL = f"{_BASE_URL}/wjw/gfxwjj/list.shtml"
_MAX_PAGES = 200
_BS_PARSERS = ("html5lib", "lxml", "html.parser")


# ---------------------------------------------------------------------------
# HTML utilities
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback parsers; return BeautifulSoup or None."""
    for parser in _BS_PARSERS:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(text: str) -> str:
    """Remove HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Playwright helper
# ---------------------------------------------------------------------------

def _playwright_fetch(url: str, retries: int = 3, block_resources: bool = True) -> str | None:
    """Fetch a URL with Playwright (headless Chromium). Returns HTML or None.

    Uses exponential backoff: 1 s, 3 s, 9 s between attempts.
    """
    for attempt in range(retries):
        if attempt > 0:
            wait = (3 ** attempt)  # 3s, 9s
            print(f"[nhc-gov-cn-wjw] playwright retry {attempt}/{retries - 1} "
                  f"for {url} in {wait}s...", flush=True)
            time.sleep(wait)
        try:
            from crawler.playwright_fetcher import fetch_html
            html = fetch_html(
                url,
                timeout_seconds=45,
                extra_wait_seconds=4.0,
                block_resources=block_resources,
            )
            if html and len(html) > 1000:
                return html
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print(f"[nhc-gov-cn-wjw] playwright error (attempt {attempt + 1}): {exc}",
                  flush=True)
    return None


# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

def _parse_list_page(html: str) -> list[tuple[str, str, str]]:
    """Return list of (full_url, title, listed_date) from a list HTML page.

    Only includes article URLs matching the NHC YYYYMM/HEX_ID.shtml pattern.
    """
    soup = _make_soup(html)
    if soup is None:
        return []

    items: list[tuple[str, str, str]] = []
    for li in soup.find_all("li"):
        a = li.find("a", href=True)
        if not a:
            continue
        href = (a.get("href") or "").strip()
        if not href or "javascript" in href or ".shtml" not in href:
            continue
        # Only article URLs: /DEPT/cNUM/YYYYMM/HEXID.shtml
        if not re.search(r"/\d{6}/[0-9a-f]{32}\.shtml", href):
            continue
        title = (a.get("title") or a.get_text(strip=True) or "").strip()
        if not title:
            continue
        span = li.find("span", class_="ml")
        listed_date = span.get_text(strip=True) if span else ""

        if href.startswith("/"):
            url = _BASE_URL + href
        elif href.startswith("http"):
            url = href
        else:
            url = _BASE_URL + "/" + href

        items.append((url, title, listed_date))
    return items


def _parse_detail_page(html: str, page_url: str) -> dict:
    """Extract structured fields from a NHC detail page."""
    result: dict = {
        "title": "",
        "published_date": "",
        "publisher": "",
        "category": "",
        "abstract": "",
        "pdf_url": None,
        "original_filename": None,
        "file_links": [],
    }

    # --- Meta tags ---
    for field, meta_name in [
        ("title", "ArticleTitle"),
        ("published_date", "PubDate"),
        ("publisher", "SiteName"),
        ("category", "ColumnName"),
    ]:
        m = re.search(
            rf'<meta\s+name="{meta_name}"\s+content="([^"]*)"',
            html,
            re.IGNORECASE,
        )
        if m:
            val = m.group(1).strip()
            result[field] = val[:10] if field == "published_date" else val

    # --- Article body --- (div#xw_box is the NHC standard article container)
    soup = _make_soup(html)
    if soup is not None:
        content_node = (
            soup.find(id="xw_box")
            or soup.find(class_="news_content")
            or soup.find(class_="p_content")
            or soup.find(class_="con")
            or soup.find("article")
        )
        if content_node is None:
            # Fallback: largest div by text length
            best, best_len = None, 0
            for d in soup.find_all("div"):
                txt = d.get_text(strip=True)
                if len(txt) > best_len:
                    best_len, best = len(txt), d
            content_node = best

        if content_node is not None:
            paras: list[str] = []
            for p in content_node.find_all("p"):
                text = re.sub(r"\s+", " ",
                              p.get_text(separator=" ", strip=True)).strip()
                if len(text) > 5:
                    paras.append(text)
            if paras:
                result["abstract"] = "\n".join(paras)
            else:
                # Fallback: dump all text from content node
                fallback = re.sub(
                    r"\s+", " ",
                    content_node.get_text(separator=" ", strip=True),
                ).strip()
                result["abstract"] = fallback

        # Publisher from 来源 section (fallback to meta SiteName already set)
        src = soup.find(string=re.compile(r"来源"))
        if src and src.parent and not result["publisher"]:
            raw = src.parent.get_text(separator="", strip=True)
            clean = re.sub(r"来源[：:]?", "", raw).strip()
            if clean:
                result["publisher"] = clean

    # --- Attachment / file links ---
    # Relative links look like: HEXID/files/filename.pdf
    hex_id_m = re.search(r"/([0-9a-f]{32})\.shtml", page_url)
    doc_hex = hex_id_m.group(1) if hex_id_m else ""
    base_dir = page_url.rsplit("/", 1)[0]  # everything before HEXID.shtml

    file_links_seen: list[str] = []
    if doc_hex:
        for rel, fname in re.findall(
            rf'href=["\']({re.escape(doc_hex)}/files/([^"\'<>\s]+))["\']',
            html,
            re.IGNORECASE,
        ):
            full = f"{base_dir}/{rel}"
            file_links_seen.append(full)
            if fname.lower().endswith(".pdf") and result["pdf_url"] is None:
                result["pdf_url"] = full
                result["original_filename"] = fname
            elif result["original_filename"] is None:
                result["original_filename"] = fname

    result["file_links"] = file_links_seen
    return result


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class NHCGovCnWjwCrawler(BaseCrawler):
    """Crawler for NHC 规范性文件 (Normative Documents)."""

    site_id = "nhc-gov-cn-wjw"
    site_name = "Custom: nhc-gov-cn-wjw"
    base_url = _BASE_URL

    def crawl(self, limit: int | None = None) -> int:
        """Crawl NHC 规范性文件 listing.

        Parameters
        ----------
        limit:
            Maximum number of records to save.  ``None`` = unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        crawl_start = time.monotonic()
        MAX_WALL = 25 * 60  # 25-minute wall-clock cap

        limit_str = str(limit) if limit is not None else "unlimited"
        print(f"[nhc-gov-cn-wjw] Starting crawl, limit={limit_str}", flush=True)

        for page_num in range(1, _MAX_PAGES + 1):

            # --- Wall-clock guard ---
            elapsed = time.monotonic() - crawl_start
            if elapsed > MAX_WALL:
                print(f"[nhc-gov-cn-wjw] 25-minute wall-clock limit reached, stopping.",
                      flush=True)
                break

            # --- Limit guard ---
            if limit is not None and saved >= limit:
                break

            # --- Safety-cap log ---
            if page_num == _MAX_PAGES:
                print(f"[nhc-gov-cn-wjw] Safety cap of {_MAX_PAGES} pages reached.",
                      flush=True)

            # --- Progress log every 10 pages ---
            if page_num % 10 == 0:
                print(f"[nhc-gov-cn-wjw] page {page_num}: saved {saved}/{limit_str}",
                      flush=True)

            # --- Build list URL ---
            list_url = (
                _LIST_URL
                if page_num == 1
                else f"{_BASE_URL}/wjw/gfxwjj/list_{page_num}.shtml"
            )

            # --- Fetch list page (3 retries, exponential backoff) ---
            list_html: str | None = None
            for attempt in range(3):
                if attempt > 0:
                    w = attempt * 3  # 3 s, 6 s
                    print(f"[nhc-gov-cn-wjw] list page retry {attempt}/2 "
                          f"for page {page_num} in {w}s...", flush=True)
                    time.sleep(w)
                try:
                    list_html = _playwright_fetch(list_url, retries=1, block_resources=True)
                    if list_html and len(list_html) > 1000:
                        break
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nhc-gov-cn-wjw] list page fetch error: {exc}", flush=True)

            if not list_html or len(list_html) < 1000:
                print(f"[nhc-gov-cn-wjw] Failed to load list page {page_num}, stopping.",
                      flush=True)
                break

            # --- Parse list page ---
            items = _parse_list_page(list_html)
            if not items:
                print(f"[nhc-gov-cn-wjw] No items on page {page_num}, done.", flush=True)
                break

            # --- URL deduplication (guards against paginator looping back) ---
            new_items = [(u, t, d) for u, t, d in items if u not in seen_urls]
            if not new_items:
                print(f"[nhc-gov-cn-wjw] All items on page {page_num} already seen, "
                      f"stopping.", flush=True)
                break

            # --- Per-item fetch + parse + save ---
            for url, title, listed_date in new_items:
                if limit is not None and saved >= limit:
                    break
                if url in seen_urls:
                    continue
                seen_urls.add(url)

                try:
                    # Fetch detail page
                    detail_html: str | None = None
                    for attempt in range(3):
                        if attempt > 0:
                            w = 3 ** attempt  # 3 s, 9 s
                            print(f"[nhc-gov-cn-wjw] detail retry {attempt}/2 "
                                  f"for {url} in {w}s...", flush=True)
                            time.sleep(w)
                        try:
                            detail_html = _playwright_fetch(
                                url, retries=1, block_resources=True
                            )
                            if detail_html and len(detail_html) > 1000:
                                break
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            print(f"[nhc-gov-cn-wjw] detail fetch error: {exc}",
                                  flush=True)

                    if not detail_html or len(detail_html) < 1000:
                        print(f"[nhc-gov-cn-wjw] Empty detail page for {url}, skipping.",
                              flush=True)
                        continue

                    # Parse detail
                    detail = _parse_detail_page(detail_html, url)

                    # Abstract quality gate (requirement: skip if < 50 chars)
                    abstract = (detail.get("abstract") or "").strip()
                    if len(abstract) < 50:
                        print(f"[nhc-gov-cn-wjw] Short abstract ({len(abstract)} chars) "
                              f"for {url}, skipping.", flush=True)
                        continue

                    # External / post IDs (hex UUID from URL path)
                    hex_m = re.search(r"/([0-9a-f]{32})\.shtml", url)
                    ext_id = hex_m.group(1) if hex_m else url.split("/")[-1].replace(".shtml", "")

                    final_title = detail["title"] or title
                    pub_date = detail["published_date"] or listed_date[:10] if listed_date else ""

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "post_number": ext_id,  # no numeric post# — use hex UUID
                        "title": final_title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "url": url,
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "publisher": detail["publisher"] or None,
                        "department": detail["publisher"] or None,
                        "category": detail["category"] or None,
                        "keywords": None,
                        "authors": None,
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "originalFilename": detail["original_filename"],
                                "columnName": detail["category"],
                                "siteName": detail["publisher"],
                                "fileLinks": detail["file_links"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[nhc-gov-cn-wjw] saved {saved}/{limit_str}: "
                        f"{final_title[:60]}",
                        flush=True,
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[nhc-gov-cn-wjw] item {url} failed: {exc}", flush=True)
                    continue

                time.sleep(self._delay)

        print(f"[nhc-gov-cn-wjw] Done. Total saved: {saved}", flush=True)
        return saved
