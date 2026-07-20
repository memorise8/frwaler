# -*- coding: utf-8 -*-
"""KINU (통일연구원) 보도자료 board crawler.

Target: https://www.kinu.or.kr/main/board/index.do?nav_code=mai1674793705&code=XDXh8TgkVFNJ
"""

import json
import re
import subprocess
import time
import sys
import os
from html import unescape

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))
from crawler.base_crawler import BaseCrawler

_NAV_CODE = "mai1674793705"
_CODE = "XDXh8TgkVFNJ"
_BASE = "https://www.kinu.or.kr"
_LIST_URL = f"{_BASE}/main/board/index.do"
_VIEW_URL = f"{_BASE}/main/board/view.do"
_MAX_PAGES = 200
_WALL_SECONDS = 25 * 60
_MIN_ABSTRACT = 100  # skip items with shorter abstracts


def _curl_get(url, retries=3):
    """GET via curl with TLS 1.3; returns decoded text or None on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "-L",
        "--max-time", "30",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    wait_times = [1, 3, 9]
    for attempt, wait in enumerate(wait_times, start=1):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35, check=False)
            raw = result.stdout
            if raw and raw.strip():
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            if attempt == len(wait_times):
                print(f"[kinu-or-kr-main] curl failed: {exc}")
                return None
            print(f"[kinu-or-kr-main] curl error attempt {attempt}: {exc}, retrying in {wait}s")
            time.sleep(wait)
            continue

        if attempt < len(wait_times):
            print(f"[kinu-or-kr-main] empty response attempt {attempt}, retrying in {wait}s")
            time.sleep(wait)
    return None


def _bs(html_text):
    """Parse HTML with fallback chain html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html_text, parser)
        except Exception:
            continue
    return None


def _clean(text):
    if not text:
        return ""
    text = unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _parse_list_ids(html_text):
    """Extract board IDs from list page HTML using viewBoard(ID) pattern."""
    return re.findall(r"viewBoard\((\d+)\)", html_text)


def _parse_detail(html_text, board_id):
    """
    Parse a detail page. Returns dict with title/abstract/date/pdf_url or None.
    Parses: div.board-view → div.subject (title), div.date, div.text (body).
    """
    try:
        soup = _bs(html_text)
    except Exception as exc:
        print(f"[kinu-or-kr-main] BeautifulSoup failed for id={board_id}: {exc}")
        return None

    if soup is None:
        return None

    # Title from div.subject inside div.board-view
    board_view = soup.find(class_="board-view")
    subject_el = None
    if board_view:
        subject_el = board_view.find(class_="subject")
    if subject_el is None:
        subject_el = soup.find(class_="subject")

    if subject_el is None:
        return None

    title = _clean(subject_el.get_text(separator=" ", strip=True))
    if not title:
        return None

    # Date: div.date
    published_date = None
    date_el = soup.find(class_="date")
    if date_el:
        raw_date = _clean(date_el.get_text())
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", raw_date)
        if m:
            published_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    # Abstract: div.text content inside board-view-cont
    abstract = ""
    text_div = soup.find("div", class_="text")
    if text_div:
        abstract = text_div.get_text(separator="\n", strip=True)

    if not abstract:
        cont = soup.find(class_="board-view-cont")
        if cont:
            abstract = cont.get_text(separator="\n", strip=True)

    # Normalize whitespace
    abstract = re.sub(r"[ \t]+", " ", abstract)
    abstract = re.sub(r"\n{3,}", "\n\n", abstract).strip()

    # PDF URL from file download link
    pdf_url = None
    dl_link = soup.find("a", href=re.compile(r"/main/board/boardFile/download/"))
    if dl_link:
        href = dl_link.get("href", "")
        pdf_url = (_BASE + href) if href.startswith("/") else href

    return {
        "title": title,
        "abstract": abstract,
        "published_date": published_date,
        "pdf_url": pdf_url,
    }


class KinuOrKrMainCrawler(BaseCrawler):
    site_id = "kinu-or-kr-main"
    site_name = "Custom: kinu-or-kr-main"
    base_url = _BASE

    def crawl(self, limit=None):
        saved = 0
        seen_ids = set()
        limit_display = limit if limit is not None else "∞"
        start_time = time.time()

        page = 1

        while page <= _MAX_PAGES:
            # Wall-clock budget
            if time.time() - start_time > _WALL_SECONDS:
                print(f"[kinu-or-kr-main] 25-min wall-clock budget reached at page {page}, stopping.")
                break

            if page % 10 == 0:
                print(f"[kinu-or-kr-main] page {page}: saved {saved}/{limit_display}")

            list_url = (
                f"{_LIST_URL}?nav_code={_NAV_CODE}&code={_CODE}&viewPage={page}"
            )
            raw = _curl_get(list_url)
            if not raw:
                print(f"[kinu-or-kr-main] page {page}: empty response, stopping.")
                break

            board_ids = _parse_list_ids(raw)
            if not board_ids:
                print(f"[kinu-or-kr-main] page {page}: no items found, end of pagination.")
                break

            new_ids = [bid for bid in board_ids if bid not in seen_ids]
            for bid in board_ids:
                seen_ids.add(bid)

            if not new_ids and page > 1:
                print(f"[kinu-or-kr-main] page {page}: all items already seen, stopping.")
                break

            if page == _MAX_PAGES:
                print(f"[kinu-or-kr-main] safety cap of {_MAX_PAGES} pages reached.")

            for bid in new_ids:
                if limit is not None and saved >= limit:
                    break

                detail_url = (
                    f"{_VIEW_URL}?nav_code={_NAV_CODE}&code={_CODE}&idx={bid}"
                )

                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(detail_url)
                    if not detail_raw:
                        print(f"[kinu-or-kr-main] item {bid} failed: empty detail response")
                        continue

                    parsed = _parse_detail(detail_raw, bid)
                    if parsed is None:
                        print(f"[kinu-or-kr-main] item {bid} failed: could not parse detail page")
                        continue

                    abstract = parsed["abstract"]
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[kinu-or-kr-main] item {bid} skipped: abstract too short "
                            f"({len(abstract)} chars): {parsed['title'][:60]}"
                        )
                        continue

                    self._save_paper({
                        "id": f"{self.site_id}-{bid}",
                        "site_id": self.site_id,
                        "external_id": bid,
                        "title": parsed["title"],
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "보도자료",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": parsed["published_date"],
                        "url": detail_url,
                        "pdf_url": parsed["pdf_url"],
                        "doi": None,
                        "department": "통일연구원",
                        "metadata": json.dumps(
                            {"board_id": bid, "code": _CODE, "nav_code": _NAV_CODE},
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kinu-or-kr-main] item {bid} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            page += 1

        print(f"[kinu-or-kr-main] crawl complete: saved {saved} items across {page} pages.")
        return saved
