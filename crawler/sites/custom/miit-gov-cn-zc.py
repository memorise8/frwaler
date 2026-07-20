# -*- coding: utf-8 -*-
"""
Crawler for MIIT 行政规范性文件 (Administrative Normative Documents).
Source: https://www.miit.gov.cn/zc/wjxzfl/xzgfxwj/index.html
API:    https://www.miit.gov.cn/search-front-server/api/search/info
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_BASE = "https://www.miit.gov.cn"
_LIST_API = f"{_BASE}/search-front-server/api/search/info"
_LIST_REFERER = f"{_BASE}/search/xzgfxwjnew/index.html"
_DETAIL_REFERER = f"{_BASE}/zc/wjxzfl/xzgfxwj/index.html"
_PAGE_SIZE = 15
_RATE_SLEEP = 1.0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _curl(url, referer=_BASE, retries=3):
    """Fetch URL via curl (handles Chinese gov TLS quirks). Returns bytes."""
    delay = 1
    for attempt in range(retries):
        try:
            res = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-sk",
                    "-A", _UA,
                    "-H", f"Referer: {referer}",
                    "-H", "Accept-Language: zh-CN,zh;q=0.9",
                    "--max-time", "30",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            if res.returncode == 0 and res.stdout:
                return res.stdout
        except Exception as exc:
            print(f"[miit-gov-cn-zc] curl attempt {attempt + 1}/{retries} failed: {exc}")
        if attempt < retries - 1:
            time.sleep(delay)
            delay *= 3
    return b""


# ---------------------------------------------------------------------------
# Date helpers
# ---------------------------------------------------------------------------

def _ms_to_date(ts_ms):
    """Convert millisecond UNIX timestamp to ISO date string (YYYY-MM-DD)."""
    if not ts_ms:
        return None
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

def _make_soup(html_bytes):
    """Parse HTML bytes with html5lib → lxml → html.parser fallback chain."""
    try:
        html = html_bytes.decode("utf-8", errors="replace")
    except Exception:
        html = html_bytes.decode("latin-1", errors="replace")

    from bs4 import BeautifulSoup  # always available in this venv
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_detail(html_bytes):
    """
    Extract structured data from a MIIT document detail page.

    Returns a dict with keys:
        abstract, keywords, publisher, published_date, pdf_url, original_filename
    """
    result = {
        "abstract": "",
        "keywords": None,
        "publisher": None,
        "published_date": None,
        "pdf_url": None,
        "original_filename": None,
    }

    if not html_bytes:
        return result

    try:
        soup = _make_soup(html_bytes)
    except Exception:
        soup = None

    if soup is None:
        # Last-resort regex fallback
        html_str = html_bytes.decode("utf-8", errors="replace")
        m = re.search(r'<meta[^>]+name=["\']Description["\'][^>]+content=["\']([^"\']+)["\']', html_str, re.I)
        if m:
            result["abstract"] = m.group(1).strip()
        return result

    # --- Extract meta tags ---
    for meta in soup.find_all("meta"):
        name = (meta.get("name") or "").strip().lower()
        content = (meta.get("content") or "").strip()
        if not content:
            continue
        if name == "description":
            result["abstract"] = content
        elif name == "keywords":
            result["keywords"] = content
        elif name == "contentsource":
            result["publisher"] = content
        elif name == "pubdate":
            # "2026-04-03 16:48" -> "2026-04-03"
            result["published_date"] = content[:10]

    # --- Extract PDF URL from #con_con iframe ---
    con_div = soup.find(id="con_con")
    if con_div:
        iframe = con_div.find("iframe")
        if iframe:
            # "fileurl" attribute is the clean PDF path; "src" may be a viewer URL
            fileurl = (iframe.get("fileurl") or "").strip()
            src = (iframe.get("src") or "").strip()

            # Prefer fileurl; fall back to extracting file= param from src
            pdf_path = fileurl
            if not pdf_path and "file=" in src:
                pdf_path = re.sub(r".*[?&]file=", "", src).split("&")[0]
            if not pdf_path:
                pdf_path = src

            if pdf_path and ".pdf" in pdf_path.lower():
                if pdf_path.startswith("/"):
                    result["pdf_url"] = _BASE + pdf_path
                elif pdf_path.startswith("http"):
                    result["pdf_url"] = pdf_path
                if result["pdf_url"]:
                    result["original_filename"] = pdf_path.rstrip("/").split("/")[-1].split("?")[0]

        # If abstract is still short, try body text from con_con
        if len(result["abstract"]) < 100:
            body = con_div.get_text(separator=" ", strip=True)
            body = re.sub(r"\s+", " ", body).strip()
            if len(body) > len(result["abstract"]):
                result["abstract"] = body[:3000]

    return result


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class MiitGovCnZcCrawler(BaseCrawler):
    site_id = "miit-gov-cn-zc"
    site_name = "Custom: miit-gov-cn-zc"
    base_url = "https://www.miit.gov.cn"

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "inf"
        start_time = time.time()
        MAX_WALL = 25 * 60   # 25-minute hard budget
        MAX_PAGES = 200

        page = 1

        while True:
            # --- Time budget ---
            if time.time() - start_time > MAX_WALL:
                print(f"[miit-gov-cn-zc] 25-minute wall budget reached, stopping.")
                break

            # --- Safety cap ---
            if page > MAX_PAGES:
                print(f"[miit-gov-cn-zc] Safety cap of {MAX_PAGES} pages reached, stopping.")
                break

            # --- Limit satisfied ---
            if limit is not None and saved >= limit:
                break

            # --- Progress log ---
            if page == 1 or page % 10 == 0:
                print(f"[miit-gov-cn-zc] page {page}: saved {saved}/{limit_str}")

            # --- Fetch list page from API ---
            api_url = (
                f"{_LIST_API}?"
                f"websiteid=110000000000000"
                f"&scope=basic"
                f"&q="
                f"&pg={_PAGE_SIZE}"
                f"&cateid=194"
                f"&pos=title_text%2Ctitlepy"
                f"&_cus_lq_themename=%E8%A1%8C%E6%94%BF%E8%A7%84%E8%8C%83%E6%80%A7%E6%96%87%E4%BB%B6"
                f"&dateField=deploytime"
                f"&selectFields=title%2Cdeploytime%2Curl%2Ccdate%2Cfilenumbername%2C"
                f"publishgroupname%2Cpublishtime%2Cmetaid%2Ccolumnid%2Ccreatedate"
                f"&group=distinct"
                f"&level=6"
                f"&sortFields=%5B%7B%22name%22%3A%22deploytime%22%2C%22type%22%3A%22desc%22%7D%5D"
                f"&p={page}"
            )

            raw = _curl(api_url, referer=_LIST_REFERER)
            if not raw:
                print(f"[miit-gov-cn-zc] Empty response on page {page}, stopping.")
                break

            try:
                resp = json.loads(raw.decode("utf-8", errors="replace"))
                results = resp["data"]["searchResult"]["dataResults"]
            except Exception as exc:
                print(f"[miit-gov-cn-zc] JSON parse error on page {page}: {exc}")
                break

            if not results:
                print(f"[miit-gov-cn-zc] No results on page {page}, done.")
                break

            new_on_page = 0

            for item_rec in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    # Unwrap groupData nesting
                    gd = item_rec.get("groupData") or []
                    record = gd[0] if gd else item_rec
                    d = record.get("data") if isinstance(record, dict) else {}
                    if not isinstance(d, dict):
                        d = {}

                    item_url = (d.get("url") or "").strip()
                    if not item_url:
                        continue

                    full_url = _BASE + item_url if item_url.startswith("/") else item_url

                    if full_url in seen_urls:
                        continue
                    seen_urls.add(full_url)
                    new_on_page += 1

                    title = (d.get("title") or "").strip()
                    if not title:
                        continue

                    # Extract external_id from URL (art_<uuid>.html)
                    m = re.search(r"art_([0-9a-f]{10,})", item_url)
                    external_id = m.group(1) if m else item_url.split("/")[-1].replace(".html", "")

                    # Timestamps from API
                    deploytime_ms = d.get("deploytime")   # listed_date source
                    publishtime_ms = d.get("publishtime")  # published_date fallback

                    listed_date = _ms_to_date(deploytime_ms)
                    published_date_fallback = _ms_to_date(publishtime_ms) or listed_date

                    # --- Fetch detail page ---
                    detail_html = _curl(full_url, referer=_DETAIL_REFERER)
                    detail = _parse_detail(detail_html)
                    time.sleep(_RATE_SLEEP)

                    abstract = detail["abstract"]
                    if len(abstract) < 50:
                        print(
                            f"[miit-gov-cn-zc] abstract too short ({len(abstract)} chars) "
                            f"for {full_url}, skipping."
                        )
                        continue

                    published_date = detail["published_date"] or published_date_fallback
                    publisher = (detail["publisher"] or d.get("publishgroupname") or "").strip()

                    meta_payload = {
                        "filenumbername": d.get("filenumbername"),
                        "publishgroupname": d.get("publishgroupname"),
                        "columnid": d.get("columnid"),
                        "deploytime_ms": deploytime_ms,
                        "publishtime_ms": publishtime_ms,
                        "originalFilename": detail["original_filename"],
                        "posted_date": listed_date,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": str(deploytime_ms) if deploytime_ms else None,
                        "url": full_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "authors": None,
                        "publisher": publisher or None,
                        "department": None,
                        "journal": None,
                        "pdf_url": detail["pdf_url"],
                        "keywords": detail["keywords"],
                        "category": "行政规范性文件",
                        "doi": None,
                        "original_filename": detail["original_filename"],
                        "metadata": json.dumps(meta_payload, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    url_hint = item_url if "item_url" in dir() else "?"
                    print(f"[miit-gov-cn-zc] item {url_hint} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            if new_on_page == 0:
                print(f"[miit-gov-cn-zc] No new items on page {page}, stopping.")
                break

            page += 1

        print(f"[miit-gov-cn-zc] crawl complete. saved={saved}")
        return saved
