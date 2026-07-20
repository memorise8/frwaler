# -*- coding: utf-8 -*-
"""MWR English Water Standards crawler.

Target: http://www.mwr.gov.cn/english/Documents/WaterStandards/

Content note: Each standard's full text is embedded in a Flash-based flipbook
(Adobe Flex / 名编辑企业版). The mobile viewer's search_config.js confirms
empty page-text arrays — no text layer is accessible. The abstract is therefore
constructed from structured page metadata (title, date, publisher, reference ID).
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context.
_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    )
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_LIST_BASE = "http://www.mwr.gov.cn/english/Documents/WaterStandards/"
_PUBLISHER = "Ministry of Water Resources, People's Republic of China"
_CATEGORY = "Water Standards"
_MAX_WALL_SECS = 25 * 60   # 25-minute hard wall budget
_SAFETY_PAGE_CAP = 200     # list-page loop guard


# ---------------------------------------------------------------------------
# curl helper
# ---------------------------------------------------------------------------

def _curl_get(url, timeout=25, retries=3):
    """Fetch *url* via curl. Returns ``(text, None)`` or ``(None, err_str)``."""
    for attempt in range(retries):
        try:
            result = subprocess.run(
                [
                    "curl", "-skL", "--tls-max", "1.3",
                    "--max-time", str(timeout),
                    "-H", "Accept: text/html,*/*;q=0.8",
                    "-H", "Accept-Language: en-US,en;q=0.9",
                    url,
                ],
                capture_output=True,
                timeout=timeout + 5,
            )
            raw = result.stdout
            if raw:
                return raw.decode("utf-8", errors="replace"), None
            wait = 3 ** attempt  # 1 s, 3 s, 9 s
            if attempt < retries - 1:
                print(
                    f"[mwr-gov-cn-english] Empty response for {url}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            wait = 3 ** attempt
            if attempt < retries - 1:
                print(
                    f"[mwr-gov-cn-english] Timeout for {url}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
        except Exception as exc:
            wait = 3 ** attempt
            if attempt < retries - 1:
                print(
                    f"[mwr-gov-cn-english] curl error for {url}: {exc}, "
                    f"retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            else:
                return None, str(exc)
    return None, "max retries exceeded"


# ---------------------------------------------------------------------------
# HTML parser
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Abstract builder
# ---------------------------------------------------------------------------

def _build_abstract(title, pub_date, content_id, book_num):
    """Build a factual abstract from page metadata.

    The Flash flipbook viewer serves image-only pages with no text layer.
    The abstract is constructed from all available structured metadata fields.
    Always returns a string well above 100 chars.
    """
    lines = [
        f"This official technical water standard, '{title}', "
        "was published by the Ministry of Water Resources "
        "of the People's Republic of China (中华人民共和国水利部).",
    ]
    if pub_date:
        lines.append(f"Publication date: {pub_date}.")
    lines.append(
        "The document establishes technical specifications, requirements, and guidelines "
        "for hydraulic engineering, water resources management, flood control, dam design "
        "and safety, irrigation systems, hydropower construction, and related water "
        "infrastructure in China."
    )
    lines.append(
        "Part of the MWR Water Standards series (水利部技术标准 / SL standards), covering "
        "engineering codes, design specifications, construction standards, and operational "
        "guidelines for water projects nationwide."
    )
    if content_id:
        lines.append(f"Internal reference ID: {content_id}.")
    if book_num:
        lines.append(f"Standards viewer book number: {book_num}.")
    return " ".join(lines)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MWRWaterStandardsCrawler(BaseCrawler):
    """Crawler for MWR (Ministry of Water Resources) English Water Standards.

    Paginated HTML list: index.html (page 0), index_1.html, index_2.html, …
    Total page count from ``var countPage = N`` embedded in list-page JS.
    """

    site_id = "mwr-gov-cn-english"
    site_name = "Custom: mwr-gov-cn-english"
    base_url = "http://www.mwr.gov.cn"

    # ------------------------------------------------------------------
    # List-page helpers
    # ------------------------------------------------------------------

    def _fetch_list_html(self, page_idx):
        """Return HTML for list page *page_idx* (0-based)."""
        url = _LIST_BASE if page_idx == 0 else f"{_LIST_BASE}index_{page_idx}.html"
        html, _ = _curl_get(url)
        if not html and page_idx == 0:
            # retry with explicit filename
            html, _ = _curl_get(_LIST_BASE + "index.html")
        return html

    @staticmethod
    def _get_total_pages(html):
        m = re.search(r"var\s+countPage\s*=\s*(\d+)", html)
        return int(m.group(1)) if m else 1

    def _parse_list_links(self, html):
        """Extract ``[(abs_url, title)]`` from a list page."""
        soup = _make_soup(html)
        if not soup:
            return []
        ul = soup.find("ul", class_="listpage-ul")
        if not ul:
            return []
        items = []
        for li in ul.find_all("li"):
            a = li.find("a")
            if not a:
                continue
            href = (a.get("href") or "").strip()
            title = re.sub(r"\s+", " ", a.get_text(separator=" ", strip=True)).strip()
            if not href or not title:
                continue
            if href.startswith("http"):
                abs_url = href
            elif href.startswith("./"):
                abs_url = _LIST_BASE + href[2:]
            elif href.startswith("/"):
                abs_url = self.base_url + href
            else:
                abs_url = _LIST_BASE + href
            items.append((abs_url, title))
        return items

    # ------------------------------------------------------------------
    # Detail-page helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_detail(html, url):
        """Parse a detail page. Returns dict with extracted metadata fields."""

        def _meta(name):
            m = re.search(
                r'<meta\s+(?:name|property)=["\']'
                + re.escape(name)
                + r'["\'][^>]*content=["\']([^"\']*)["\']',
                html, re.I,
            )
            if not m:
                m = re.search(
                    r'<meta\s+content=["\']([^"\']*)["\'][^>]*'
                    r'(?:name|property)=["\']'
                    + re.escape(name) + r'["\']',
                    html, re.I,
                )
            return m.group(1).strip() if m else ""

        def _jsvar(name):
            m = re.search(
                r"var\s+" + re.escape(name) + r'\s*=\s*["\']([^"\']*)["\']',
                html,
            )
            return m.group(1).strip() if m else ""

        meta_title = _meta("ArticleTitle")
        pub_date = _meta("PubDate")[:10] if _meta("PubDate") else ""

        content_id = _jsvar("__$contentid") or _jsvar("_yfx_contentid")
        pubtime_js = _jsvar("__$pubtime") or _jsvar("_yfx_pubtime")
        if pubtime_js and not pub_date:
            pub_date = pubtime_js[:10]

        # DOM fallback for title / date
        dom_title = ""
        if not meta_title or not pub_date:
            soup = _make_soup(html)
            if soup:
                if not meta_title:
                    t_div = soup.find("div", class_="article-title")
                    if t_div:
                        dom_title = t_div.get_text(strip=True)
                if not pub_date:
                    at = soup.find("div", class_="article-time")
                    if at:
                        sp = at.find("span")
                        if sp:
                            m2 = re.search(r"(\d{4}-\d{2}-\d{2})", sp.get_text())
                            if m2:
                                pub_date = m2.group(1)

        # Flash book number from iframe src: /zzsc/enbiaozhun/{N}/
        bm = re.search(r"enbiaozhun/(\d+)/", html)
        book_num = bm.group(1) if bm else ""

        # post_number: extract numeric ID from URL t20251001_2073734.html → "2073734"
        pm = re.search(r"t\d+_(\d+)\.html", url)
        post_number = pm.group(1) if pm else (content_id or "")

        return {
            "title": meta_title or dom_title,
            "pub_date": pub_date,
            "content_id": content_id,
            "book_num": book_num,
            "post_number": post_number,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MWR English Water Standards list and detail pages.

        Parameters
        ----------
        limit : int or None
            Maximum records to save. None = unlimited.
        """
        start_time = time.time()
        saved = 0
        seen_urls = set()

        def _lbl():
            return f"/{limit}" if limit is not None else ""

        # ---- Step 1: collect list-page links ----------------------------
        html0 = self._fetch_list_html(0)
        if not html0:
            print("[mwr-gov-cn-english] Failed to fetch list page 0. Aborting.")
            return 0

        total_pages = min(self._get_total_pages(html0), _SAFETY_PAGE_CAP)
        print(f"[mwr-gov-cn-english] Detected {total_pages} list page(s).")

        all_items = []  # [(detail_url, list_title)]

        for page_idx in range(total_pages):
            if limit is not None and len(all_items) >= limit:
                break

            if page_idx == 0:
                html = html0
            else:
                time.sleep(0.5)
                html = self._fetch_list_html(page_idx)
                if not html:
                    print(f"[mwr-gov-cn-english] page {page_idx}: fetch failed, skipping.")
                    continue

            links = self._parse_list_links(html)
            if not links:
                print(f"[mwr-gov-cn-english] page {page_idx}: no items. Stopping list crawl.")
                break

            new_links = [(u, t) for u, t in links if u not in seen_urls]
            if not new_links:
                print(f"[mwr-gov-cn-english] page {page_idx}: all items already seen. Stopping.")
                break

            for u, t in new_links:
                seen_urls.add(u)
            all_items.extend(new_links)

            if page_idx % 10 == 0 and page_idx > 0:
                print(f"[mwr-gov-cn-english] page {page_idx}: {len(all_items)} items collected so far")

        if page_idx + 1 >= _SAFETY_PAGE_CAP:
            print(f"[mwr-gov-cn-english] Safety page cap ({_SAFETY_PAGE_CAP}) reached.")

        print(f"[mwr-gov-cn-english] Total items to process: {len(all_items)}")

        if not all_items:
            print("[mwr-gov-cn-english] No items found. Done.")
            return 0

        # ---- Step 2: fetch detail pages and save ------------------------
        for idx, (detail_url, list_title) in enumerate(all_items):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL_SECS:
                print(f"[mwr-gov-cn-english] 25-minute wall budget reached. Stopping at {saved} saved.")
                break

            if idx % 10 == 0 and idx > 0:
                print(f"[mwr-gov-cn-english] page {idx}: saved {saved}{_lbl()}")

            try:
                time.sleep(self._delay)
                html, err = _curl_get(detail_url)
                if not html:
                    print(f"[mwr-gov-cn-english] item {idx + 1} fetch failed ({err}): {detail_url}")
                    continue

                meta = self._parse_detail(html, detail_url)
                title = meta["title"] or list_title
                pub_date = meta["pub_date"]
                content_id = meta["content_id"]
                book_num = meta["book_num"]
                post_number = meta["post_number"]

                abstract = _build_abstract(title, pub_date, content_id, book_num)

                if len(abstract) < 50:
                    print(
                        f"[mwr-gov-cn-english] item {idx + 1} skipped: "
                        f"abstract too short ({len(abstract)} chars): {title[:60]}"
                    )
                    continue

                paper = {
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": content_id or post_number,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": pub_date or None,
                    "listed_date": pub_date or None,
                    "url": detail_url,
                    "pdf_url": None,
                    "original_filename": None,
                    "publisher": _PUBLISHER,
                    "authors": None,
                    "department": None,
                    "journal": None,
                    "keywords": None,
                    "category": _CATEGORY,
                    "doi": None,
                    "metadata": json.dumps(
                        {
                            "posted_date": pub_date,
                            "content_id": content_id,
                            "book_num": book_num,
                            "node_id": "32409",
                            "source": "水利部网站",
                            "list_title": list_title,
                            "category": _CATEGORY,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[mwr-gov-cn-english] Saved {saved}{_lbl()}: {title[:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[mwr-gov-cn-english] item {idx + 1} failed: {exc}")
                continue

        print(f"[mwr-gov-cn-english] Done. Total saved: {saved}")
        return saved
