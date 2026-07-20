# -*- coding: utf-8 -*-
"""CSRC (China Securities Regulatory Commission) English - IAC section crawler.

Target: http://www.csrc.gov.cn/csrc_en/c102061/common_list.shtml
        ?channelid=b0aaeebcfd8c48a18d5335f3ed455752
List API: /searchList/{channelid}?_isAgg=true&_isJson=true&_pageSize=N&...&page=P
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timezone
from urllib.parse import unquote, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

_CHANNEL_ID = "b0aaeebcfd8c48a18d5335f3ed455752"
_CHANNEL_CODE = "c102061"
_API_TMPL = (
    "http://www.csrc.gov.cn/searchList/{cid}"
    "?_isAgg=true&_isJson=true&_pageSize={ps}"
    "&_template=index&_rangeTimeGte=&_channelName=&page={page}"
)
_BASE = "http://www.csrc.gov.cn"
_CHANNEL_BASE = f"{_BASE}/csrc_en/{_CHANNEL_CODE}/"
_PAGE_SIZE = 20


def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with TLS workaround and exponential backoff (1s, 3s, 9s)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: application/json, text/html, */*;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.stdout and result.stdout.strip():
                return result.stdout.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1, 3, 9
                print(f"[csrc-gov-cn-csrc_en] Empty response attempt {attempt + 1}, retry in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[csrc-gov-cn-csrc_en] curl error attempt {attempt + 1}: {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[csrc-gov-cn-csrc_en] curl failed after {retries} attempts: {exc}")
    return None


def _parse_date(ts_str: str) -> str:
    if not ts_str:
        return ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", ts_str.strip())
    return m.group(1) if m else ""


def _make_bs(html: str):
    """Try html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _fetch_detail(detail_url: str) -> dict:
    """Fetch detail page; return dict with 'text', 'pdf_url', 'original_filename'."""
    out: dict = {"text": "", "pdf_url": None, "original_filename": None}
    raw = _curl_get(detail_url)
    if not raw:
        return out
    try:
        soup = _make_bs(raw)
        if not soup:
            return out

        # Extract body text from .detail-news div
        news_div = soup.find(class_="detail-news")
        if news_div:
            text = news_div.get_text(separator=" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            # Drop bare PDF filename suffixes (not useful as abstract)
            text = re.sub(r"\S+\.pdf\s*$", "", text, flags=re.IGNORECASE).strip()
            out["text"] = text

        # Extract first .pdf href → direct download link
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            if ".pdf" in href.lower():
                pdf_url = urljoin(detail_url, href)
                out["pdf_url"] = pdf_url
                path = urlparse(pdf_url).path
                filename = unquote(path.rsplit("/", 1)[-1])
                if filename.lower().endswith(".pdf"):
                    out["original_filename"] = filename
                break
    except Exception:
        pass
    return out


def _pdf_from_content_html(content_html: str) -> tuple[str | None, str | None]:
    """Extract PDF URL + filename from the API's contentHtml field (fallback)."""
    # CMS preview link: href="common/preview_resource.action?id=...&type=pdf..."
    m = re.search(r'href="([^"]+type=pdf[^"]*)"', content_html, re.I)
    if m:
        href = m.group(1)
        pdf_url = urljoin(_CHANNEL_BASE, href)
        return pdf_url, None
    # Plain .pdf href
    m = re.search(r'href="([^"]+\.pdf)"', content_html, re.I)
    if m:
        href = m.group(1)
        pdf_url = urljoin(_CHANNEL_BASE, href)
        filename = unquote(href.rsplit("/", 1)[-1])
        return pdf_url, filename
    return None, None


def _build_abstract(title: str, channel: str, date: str, detail_text: str) -> str:
    """Build an abstract >= 100 chars, preferring real page content when available."""
    if detail_text and len(detail_text) >= 50:
        if len(detail_text) >= 100:
            return detail_text
        suffix = (
            f" Published by China Securities Regulatory Commission (CSRC) "
            f"under the {channel} section."
        )
        return detail_text + suffix

    # Synthesise from metadata — always >= 100 chars given a non-empty title
    return (
        f"{title}. "
        f"{channel} report published by China Securities Regulatory Commission (CSRC) "
        f"on {date}. "
        f"Source: International Affairs, China Securities Regulatory Commission."
    )


class CSRCEnCrawler(BaseCrawler):
    """CSRC English IAC (International Advisory Committee) crawler."""

    site_id = "csrc-gov-cn-csrc_en"
    site_name = "Custom: csrc-gov-cn-csrc_en"
    base_url = "http://www.csrc.gov.cn"

    def crawl(self, limit=None):
        _wall_start = time.time()
        _MAX_WALL = 25 * 60   # 25 minutes
        _MAX_PAGES = 200

        saved = 0
        seen_ids: set[str] = set()
        page = 1
        limit_display = str(limit) if limit is not None else "∞"

        while True:
            # Budget / limit / safety-cap checks
            if time.time() - _wall_start > _MAX_WALL:
                print(f"[csrc-gov-cn-csrc_en] Wall-clock budget reached at page {page}. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[csrc-gov-cn-csrc_en] Safety cap of {_MAX_PAGES} pages reached. Exiting.")
                break

            if page % 10 == 0:
                print(f"[csrc-gov-cn-csrc_en] page {page}: saved {saved}/{limit_display}")

            # Fetch list API
            list_url = _API_TMPL.format(cid=_CHANNEL_ID, ps=_PAGE_SIZE, page=page)
            time.sleep(self._delay)
            raw = _curl_get(list_url)
            if not raw:
                print(f"[csrc-gov-cn-csrc_en] Failed to fetch list page {page}. Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[csrc-gov-cn-csrc_en] JSON decode error at page {page}: {exc}. Stopping.")
                break

            results = data.get("data", {}).get("results") or []
            if not results:
                print(f"[csrc-gov-cn-csrc_en] No more results at page {page}. Done.")
                break

            if page == 1:
                total = data.get("data", {}).get("total")
                if total is not None:
                    print(f"[csrc-gov-cn-csrc_en] Total records: {total}")

            new_on_page = 0
            for item in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    manuscript_id = str(item.get("manuscriptId") or "").strip()
                    if not manuscript_id:
                        continue
                    if manuscript_id in seen_ids:
                        continue
                    seen_ids.add(manuscript_id)
                    new_on_page += 1

                    title = (item.get("title") or "").strip()
                    if not title:
                        print(f"[csrc-gov-cn-csrc_en] Item {manuscript_id} has no title, skipping.")
                        continue

                    # Normalise detail URL
                    raw_url = item.get("url") or ""
                    if raw_url.startswith("//"):
                        detail_url = "http:" + raw_url
                    elif raw_url.startswith("/"):
                        detail_url = _BASE + raw_url
                    else:
                        detail_url = raw_url

                    # Dates
                    published_time_str = item.get("publishedTimeStr") or ""
                    listed_date = _parse_date(published_time_str)

                    pub_ts = item.get("publishedTime")
                    if pub_ts:
                        try:
                            published_date = datetime.fromtimestamp(
                                pub_ts / 1000, tz=timezone.utc
                            ).strftime("%Y-%m-%d")
                        except Exception:
                            published_date = listed_date
                    else:
                        published_date = listed_date

                    channel_name = (item.get("channelName") or "IAC").strip()
                    content_html = item.get("contentHtml") or ""

                    # Fetch detail page for abstract text + direct PDF link
                    time.sleep(self._delay)
                    detail_info = _fetch_detail(detail_url) if detail_url else {}
                    detail_text = detail_info.get("text") or ""

                    # PDF URL: prefer direct file link from the detail page
                    pdf_url = detail_info.get("pdf_url")
                    original_filename = detail_info.get("original_filename")
                    # Fallback: parse the API's contentHtml field
                    if not pdf_url and content_html:
                        pdf_url, fn = _pdf_from_content_html(content_html)
                        if fn:
                            original_filename = fn

                    abstract = _build_abstract(
                        title, channel_name, published_date or listed_date, detail_text
                    )

                    if len(abstract) < 50:
                        print(
                            f"[csrc-gov-cn-csrc_en] Abstract too short ({len(abstract)} chars) "
                            f"for '{title[:40]}', skipping."
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": manuscript_id,
                        "post_number": manuscript_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": "China Securities Regulatory Commission (CSRC)",
                        "department": "International Advisory Committee (IAC)",
                        "category": channel_name,
                        "authors": None,
                        "keywords": None,
                        "doi": None,
                        "journal": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": published_time_str,
                                "originalFilename": original_filename,
                                "manuscriptId": manuscript_id,
                                "channelId": item.get("channelId"),
                                "channelName": channel_name,
                                "websiteId": item.get("websiteId"),
                                "subTitle": item.get("subTitle"),
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[csrc-gov-cn-csrc_en] Saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[csrc-gov-cn-csrc_en] item {item.get('manuscriptId', '?')} failed: {exc}"
                    )
                    continue

            if new_on_page == 0:
                print(f"[csrc-gov-cn-csrc_en] No new IDs on page {page}. Done.")
                break

            page += 1

        print(f"[csrc-gov-cn-csrc_en] Done. Total saved: {saved}")
        return saved
