# -*- coding: utf-8 -*-
"""Crawler for 国家民委 民族地区经济社会发展统计数据 (neac-gov-cn-seac).

List:   https://www.neac.gov.cn/seac/c103544/common_list.shtml  (page 1)
        https://www.neac.gov.cn/seac/c103544/common_list_N.shtml (page N≥2)
Detail: https://www.neac.gov.cn/seac/c103544/YYYYMM/NNNNNNN.shtml
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "neac-gov-cn-seac"
_BASE_URL = "https://www.neac.gov.cn"
_LIST_PAGE1 = f"{_BASE_URL}/seac/c103544/common_list.shtml"
_LIST_TMPL = f"{_BASE_URL}/seac/c103544/common_list_{{page}}.shtml"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 50

# BeautifulSoup parser preference order
_BS_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    """Try parsers in preference order; return BeautifulSoup or None."""
    for parser in _BS_PARSERS:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with TLS 1.3 max workaround. Returns decoded text or None."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "-H", f"User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        url,
    ]
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = delays[attempt]
                print(f"[{_SITE_ID}] Empty response (attempt {attempt+1}/{retries}), "
                      f"retrying in {wait}s: {url}")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = delays[attempt]
                print(f"[{_SITE_ID}] curl error (attempt {attempt+1}/{retries}): {exc}. "
                      f"Retrying in {wait}s: {url}")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_list_page(html: str) -> list[dict]:
    """Parse a list page; return list of {href, title, listed_date}."""
    items = []
    try:
        pattern = re.compile(
            r'href="(/seac/c103544/[^"]+\.shtml)"[^>]*>\s*(.*?)</a>\s*'
            r'<span[^>]*class="date"[^>]*>\[([^\]]+)\]',
            re.S,
        )
        for m in pattern.finditer(html):
            href = m.group(1).strip()
            title = re.sub(r"\s+", " ", _strip_html(m.group(2))).strip()
            listed_date = m.group(3).strip()
            if href and title:
                items.append({"href": href, "title": title, "listed_date": listed_date})
    except Exception as exc:
        print(f"[{_SITE_ID}] list page parse error: {exc}")
    return items


def _parse_detail(html: str) -> dict:
    """Parse a detail page; return dict with title, abstract, dates, publisher, etc."""
    result: dict = {}

    # --- title ---
    m = re.search(r'<meta\s+name="ArticleTitle"\s+content="([^"]+)"', html, re.I)
    if m:
        result["title"] = m.group(1).strip()

    # --- published_date from body (most accurate) ---
    m = re.search(r'日期[：:]\s*(\d{4}-\d{2}-\d{2})', html)
    if m:
        result["published_date"] = m.group(1)
    else:
        m = re.search(r'<meta\s+name="PubDate"\s+content="(\d{4}-\d{2}-\d{2})', html, re.I)
        if m:
            result["published_date"] = m.group(1)

    # --- publisher from 来源：XXX ---
    m = re.search(r'来源[：:]\s*([^&<\n\r]{1,60}?)(?:&|<|\s{2,}|\n)', html)
    if m:
        result["publisher"] = re.sub(r"\s+", " ", m.group(1)).strip()

    # --- category ---
    m = re.search(r'<meta\s+name="ColumnName"\s+content="([^"]+)"', html, re.I)
    if m:
        result["category"] = m.group(1).strip()

    # --- keywords ---
    m = re.search(r'<meta\s+name="Keywords"\s+content="([^"]*)"', html, re.I)
    if m and m.group(1).strip():
        result["keywords"] = m.group(1).strip()

    # --- abstract from <div class="p3"> ---
    abstract = ""
    try:
        soup = _make_soup(html)
        if soup:
            p3 = soup.find("div", class_="p3")
            if p3:
                abstract = re.sub(r"\s+", " ", p3.get_text(separator=" ")).strip()
    except Exception:
        pass

    if not abstract:
        # regex fallback: grab everything inside the first p3 div block
        m = re.search(r'class="p3">([\s\S]*?)</div>\s*\n?\s*</div>\s*\n?\s*</div>', html)
        if m:
            abstract = _strip_html(m.group(1))

    result["abstract"] = abstract

    # --- PDF / attachment links ---
    att = re.findall(
        r'href="(/[^"]+\.(?:pdf|doc|docx|xls|xlsx|zip))"', html, re.I
    )
    if att:
        result["pdf_url"] = _BASE_URL + att[0]
        result["original_filename"] = att[0].rstrip("/").split("/")[-1]

    return result


class NeacGovCnSeacCrawler(BaseCrawler):
    """Crawler for 国家民委 민족지역경제사회발전통계데이터."""

    site_id = "neac-gov-cn-seac"
    site_name = "Custom: neac-gov-cn-seac"
    base_url = "https://www.neac.gov.cn"

    def crawl(self, limit=None):  # noqa: C901
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        page = 1
        while True:
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")
                break

            list_url = _LIST_PAGE1 if page == 1 else _LIST_TMPL.format(page=page)

            list_html = _curl_get(list_url)
            if not list_html:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            items = _parse_list_page(list_html)
            if not items:
                print(f"[{_SITE_ID}] No items on page {page}. Done.")
                break

            # Filter already-seen URLs
            new_items = [it for it in items if it["href"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All items on page {page} already seen. Stopping.")
                break
            for it in new_items:
                seen_urls.add(it["href"])

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                href = item["href"]
                listed_date = item["listed_date"]
                list_title = item["title"]
                detail_url = _BASE_URL + href

                # post_number = numeric ID from URL path segment
                m = re.search(r"/(\d+)\.shtml$", href)
                post_number = m.group(1) if m else None
                external_id = post_number or href

                try:
                    time.sleep(self._delay)
                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] Failed to fetch detail: {detail_url}")
                        continue

                    detail = _parse_detail(detail_html)

                    title = detail.get("title") or list_title
                    abstract = detail.get("abstract", "")
                    published_date = detail.get("published_date") or listed_date
                    publisher = detail.get("publisher") or ""
                    keywords = detail.get("keywords") or ""
                    category = detail.get("category") or ""
                    pdf_url = detail.get("pdf_url") or ""
                    original_filename = detail.get("original_filename") or None

                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] Skipping (abstract {len(abstract)} chars < "
                            f"{_ABSTRACT_MIN_CHARS}): {title[:60]}"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url or None,
                        "authors": None,
                        "publisher": publisher or None,
                        "department": None,
                        "journal": None,
                        "keywords": keywords or None,
                        "category": category or None,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "source": publisher,
                                "column": category,
                                "post_number": post_number,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {href} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
