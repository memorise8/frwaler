# -*- coding: utf-8 -*-
"""Crawler for mva.gov.cn 政府信息公开年报 (MVA Government Information Disclosure Annual Reports).

Ministry of Veterans Affairs (退役军人事务部) — annual gov-info-disclosure reports listing:
https://www.mva.gov.cn/gongkai/zfxxgkpt/zfxxgknb/
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "mva-gov-cn-gongkai"
_BASE_URL = "https://www.mva.gov.cn"
_LIST_BASE = "https://www.mva.gov.cn/gongkai/zfxxgkpt/zfxxgknb"
_MAX_PAGES = 200
_ABSTRACT_MIN = 50
_ABSTRACT_TARGET = 100
_WALL_CLOCK_BUDGET = 25 * 60  # seconds


def _curl_get(url: str, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS workaround and exponential backoff."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
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
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}) {url}: {exc}")
        if attempt < retries - 1:
            wait = 1 * (3 ** attempt)  # 1s, 3s, 9s
            print(f"[{_SITE_ID}] Retrying in {wait}s...")
            time.sleep(wait)
    print(f"[{_SITE_ID}] All {retries} attempts failed for {url}")
    return None


def _make_soup(html: str):
    """BeautifulSoup with html5lib → lxml → html.parser fallback."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(fragment: str) -> str:
    """Remove HTML tags, unescape common entities, collapse whitespace."""
    text = re.sub(r"<style[^>]*>.*?</style>", " ", fragment, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_post_number(href: str) -> str | None:
    """Extract numeric article ID from URL like ./202603/t20260320_534737.html → '534737'."""
    m = re.search(r"_(\d+)\.html", href)
    return m.group(1) if m else None


def _resolve_url(href: str) -> str:
    """Resolve potentially relative href against the list base."""
    if href.startswith("http"):
        return href
    if href.startswith("./"):
        return f"{_LIST_BASE}/{href[2:]}"
    if href.startswith("/"):
        return f"{_BASE_URL}{href}"
    return f"{_LIST_BASE}/{href}"


class MvaGovCnGongkaiCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: mva-gov-cn-gongkai"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        limit_val = float("inf") if limit is None else limit
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        for page_idx in range(_MAX_PAGES):
            if time.time() - start_time > _WALL_CLOCK_BUDGET:
                print(f"[{_SITE_ID}] 25-minute budget reached at page {page_idx}, exiting")
                break

            if page_idx % 10 == 0:
                limit_label = str(limit_val) if limit_val != float("inf") else "∞"
                print(f"[{_SITE_ID}] page {page_idx}: saved {saved}/{limit_label}")

            page_url = (
                f"{_LIST_BASE}/index.html"
                if page_idx == 0
                else f"{_LIST_BASE}/index_{page_idx}.html"
            )

            html = _curl_get(page_url)
            if not html:
                print(f"[{_SITE_ID}] Could not fetch listing page {page_idx}, stopping")
                break

            soup = _make_soup(html)
            if not soup:
                print(f"[{_SITE_ID}] HTML parse failed on listing page {page_idx}, stopping")
                break

            overview = soup.find(class_="overview_right")
            if not overview:
                print(f"[{_SITE_ID}] .overview_right not found on page {page_idx}, stopping")
                break

            link_tags = overview.select("li a[href]")
            if not link_tags:
                print(f"[{_SITE_ID}] No list items on page {page_idx}, stopping")
                break

            new_on_page = 0
            for a_tag in link_tags:
                if saved >= limit_val:
                    break

                href = (a_tag.get("href") or "").strip()
                if not href:
                    continue

                detail_url = _resolve_url(href)
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                # Date from the inner <span>
                span = a_tag.find("span")
                listed_date = span.get_text(strip=True) if span else None

                # Title: prefer title attribute over text (text contains the date)
                title_attr = (a_tag.get("title") or "").strip()
                if not title_attr:
                    if span:
                        span.extract()
                    title_attr = a_tag.get_text(strip=True)

                post_number = _extract_post_number(href)
                external_id = post_number or href

                time.sleep(1.0)

                try:
                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] item {external_id} failed: could not fetch detail page")
                        continue

                    detail_soup = _make_soup(detail_html)
                    if not detail_soup:
                        print(f"[{_SITE_ID}] item {external_id} failed: detail parse error")
                        continue

                    def _meta(name: str) -> str | None:
                        tag = detail_soup.find("meta", attrs={"name": name})
                        if tag and tag.get("content"):
                            return tag["content"].strip()
                        return None

                    title = _meta("ArticleTitle") or title_attr
                    pub_date_raw = _meta("PubDate")  # "YYYY-MM-DD HH:MM"
                    published_date = pub_date_raw[:10] if pub_date_raw else listed_date
                    content_source = _meta("ContentSource")
                    keywords_raw = _meta("Keywords")  # semicolon-separated
                    description = _meta("Description") or ""

                    # Convert keywords: semicolons → commas
                    keywords = keywords_raw.replace(";", ",") if keywords_raw else None

                    # Abstract: Description meta first; fall back to article body text
                    abstract = description
                    if len(abstract) < _ABSTRACT_TARGET:
                        trs = detail_soup.find(class_="trs_editor_view")
                        if trs:
                            body_text = _strip_tags(str(trs))
                            if len(body_text) > len(abstract):
                                abstract = body_text[:3000]

                    if len(abstract) < _ABSTRACT_MIN:
                        print(
                            f"[{_SITE_ID}] item {external_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    metadata_dict = {
                        "posted_date": listed_date,
                        "pub_date_raw": pub_date_raw,
                        "post_number": post_number,
                    }

                    self._save_paper({
                        "site_id": _SITE_ID,
                        "external_id": external_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "publisher": content_source,
                        "keywords": keywords,
                        "url": detail_url,
                        "pdf_url": None,
                        "original_filename": None,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {external_id} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{_SITE_ID}] No new items on page {page_idx}, stopping pagination")
                break

            # Detect end-of-pagination via the embedded JS variable countPage
            m_cp = re.search(r"countPage\s*=\s*(\d+)", html)
            if m_cp and page_idx + 1 >= int(m_cp.group(1)):
                break

            if page_idx + 1 == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
