# -*- coding: utf-8 -*-
"""KISDI 정보통신정책연구원 BBS (보도자료) crawler.

Target: https://www.kisdi.re.kr/bbs/list.do?key=m2101113055776
List:   GET /bbs/list.do?key=m2101113055776&pageIndex=N  (16 items/page)
Detail: GET /bbs/view.do?bbsSn=N&key=m2101113055776
"""

import json
import os
import re
import subprocess
import time
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_KEY = "m2101113055776"
_LIST_URL = "https://www.kisdi.re.kr/bbs/list.do"
_VIEW_URL = "https://www.kisdi.re.kr/bbs/view.do"
_FILE_URL = "https://www.kisdi.re.kr/cmm/fileDown.do"
_PAGE_SIZE = 16
_RATE = 1.0
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))


def _bs(raw: bytes | str) -> "BeautifulSoup":
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    parsers = ["html5lib", "lxml", "html.parser"]
    for parser in parsers:
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    from bs4 import BeautifulSoup
    return BeautifulSoup(raw, "html.parser")


def _curl_get(url: str, params: dict | None = None) -> str | None:
    """GET via curl with TLS workaround; retries 3× with backoff."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}" if "?" not in url else f"{url}&{qs}"
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,*/*",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception as exc:
            print(f"[kisdi-re-kr-bbs] curl error (attempt {attempt+1}/3): {exc}")
        if attempt < 2:
            wait = (1, 3, 9)[attempt]
            print(f"[kisdi-re-kr-bbs] retrying in {wait}s…")
            time.sleep(wait)
    return None


def _text(tag) -> str:
    """Return stripped text of a BS4 tag, or ''."""
    if tag is None:
        return ""
    return tag.get_text(" ", strip=True)


class KisdiRekrBbsCrawler(BaseCrawler):
    """KISDI BBS (보도자료) crawler."""

    site_id = "kisdi-re-kr-bbs"
    site_name = "Custom: kisdi-re-kr-bbs"
    base_url = "https://www.kisdi.re.kr"

    def __init__(self, db_conn, delay: float = _RATE):
        super().__init__(db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl KISDI BBS and save records. Returns count saved."""
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[kisdi-re-kr-bbs] 25-minute budget reached; stopping at page {page}.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[kisdi-re-kr-bbs] page {page}: saved {saved}/{limit_label}")

            raw = _curl_get(_LIST_URL, {"key": _KEY, "pageIndex": str(page)})
            if not raw:
                print(f"[kisdi-re-kr-bbs] page {page}: empty response, stopping.")
                break

            try:
                soup = _bs(raw)
            except Exception as exc:
                print(f"[kisdi-re-kr-bbs] page {page}: parse error {exc}, skipping.")
                continue

            bbs_sns = re.findall(r"goView\('(\d+)'", raw)
            if not bbs_sns:
                print(f"[kisdi-re-kr-bbs] page {page}: no items found, end of list.")
                break

            new_on_page = 0
            for bbs_sn in bbs_sns:
                if limit is not None and saved >= limit:
                    break

                detail_url = f"{_VIEW_URL}?bbsSn={bbs_sn}&key={_KEY}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    n = self._fetch_and_save(bbs_sn, detail_url)
                    if n:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kisdi-re-kr-bbs] item {bbs_sn} failed: {exc}")
                    continue

                time.sleep(_RATE)

            if new_on_page == 0:
                print(f"[kisdi-re-kr-bbs] page {page}: all items already seen, stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[kisdi-re-kr-bbs] safety cap of {_MAX_PAGES} pages reached, stopping.")

        print(f"[kisdi-re-kr-bbs] done. saved={saved}")
        return saved

    # ------------------------------------------------------------------
    # Detail fetch + parse
    # ------------------------------------------------------------------

    def _fetch_and_save(self, bbs_sn: str, detail_url: str) -> bool:
        """Fetch detail page, parse, validate, save. Returns True if saved."""
        raw = _curl_get(detail_url)
        if not raw:
            print(f"[kisdi-re-kr-bbs] item {bbs_sn}: no response from detail page")
            return False

        try:
            soup = _bs(raw)
        except Exception as exc:
            print(f"[kisdi-re-kr-bbs] item {bbs_sn}: parse error {exc}")
            return False

        # Title
        h4 = soup.find("h4")
        title = _text(h4).strip() if h4 else ""
        if not title:
            # Fallback: look in <strong> inside li.title
            li_title = soup.find("li", class_="title")
            if li_title:
                strong = li_title.find("strong")
                title = _text(strong)
        if not title:
            print(f"[kisdi-re-kr-bbs] item {bbs_sn}: no title found, skipping")
            return False

        # Date (등록일)
        published_date = ""
        for strong_tag in soup.find_all("strong"):
            if "등록일" in _text(strong_tag):
                parent = strong_tag.parent
                date_text = _text(parent).replace("등록일", "").strip()
                published_date = date_text.replace(".", "-")[:10]
                break

        # Abstract — from <div class="content">
        content_div = soup.find("div", class_="content")
        abstract = _text(content_div).strip() if content_div else ""
        if len(abstract) < 50:
            print(
                f"[kisdi-re-kr-bbs] item {bbs_sn}: abstract too short "
                f"({len(abstract)} chars), skipping"
            )
            return False

        # PDF URL — prefer .pdf file, fall back to first attachment
        pdf_url = ""
        em_tags = soup.find_all("em")
        pdf_href = ""
        hwp_href = ""
        for em in em_tags:
            fname = em.get_text(" ", strip=True).lower()
            a_tag = em.find("a", href=True)
            if not a_tag:
                continue
            href = a_tag["href"]
            if href.startswith("/"):
                href = self.base_url + href
            if ".pdf" in fname and not pdf_href:
                pdf_href = href
            elif not hwp_href:
                hwp_href = href
        pdf_url = pdf_href or hwp_href

        # Department
        department = ""
        for strong_tag in soup.find_all("strong"):
            if "부서" in _text(strong_tag):
                span = strong_tag.find_next_sibling("span")
                if span:
                    department = _text(span)
                break

        paper = {
            "site_id": self.site_id,
            "external_id": bbs_sn,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "보도자료",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": json.dumps(
                {"bbs_sn": bbs_sn, "key": _KEY}, ensure_ascii=False
            ),
        }
        self._save_paper(paper)
        return True
