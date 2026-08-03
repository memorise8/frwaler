# -*- coding: utf-8 -*-
"""Crawler for Korean Heritage Administration (국가유산청) 주요업무통계.

Board: https://www.khs.go.kr/cop/bbs/selectBoardList.do?bbsId=BBSMSTR_1020&mn=NS_03_07_04
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "khs-go-kr-cop"
_BASE_URL = "https://www.khs.go.kr"
_BBS_ID = "BBSMSTR_1020"
_MN = "NS_03_07_04"
_LIST_URL = f"{_BASE_URL}/cop/bbs/selectBoardList.do"
_DETAIL_URL = f"{_BASE_URL}/cop/bbs/selectBoardArticle.do"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_ABSTRACT_MIN_CHARS = 50
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _strip_jsessionid(url: str) -> str:
    """Remove ;jsessionid=... from URL path segment."""
    return re.sub(r';jsessionid=[^?#&]*', '', url)


def _make_soup(html: str) -> BeautifulSoup:
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return BeautifulSoup(html, "html.parser")


class KhsGoKrCopCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: khs-go-kr-cop"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 45) -> str | None:
        """Fetch URL via curl with TLS workaround and 3-attempt retry."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt, wait in enumerate(waits, 1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_error = (
                    f"exit={result.returncode} "
                    + result.stderr.decode("utf-8", errors="replace").strip()[:120]
                )
            except Exception as exc:
                last_error = str(exc)
            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/3 failed "
                    f"({last_error}); retrying in {wait}s"
                )
                time.sleep(wait)
        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of item dicts from one board list page."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] list-page parse error: {exc}")
            return []

        table = soup.find("table", class_="tbl")
        if not table:
            return []

        items: list[dict] = []
        for row in table.find_all("tr")[1:]:  # skip header
            cells = row.find_all("td")
            if not cells:
                continue
            link = row.find("a")
            if not link:
                continue
            href = _strip_jsessionid(link.get("href", ""))
            m = re.search(r'nttId=(\d+)', href)
            if not m:
                continue

            ntt_id = m.group(1)
            post_number = cells[0].get_text(strip=True) if cells else ""
            title = link.get_text(strip=True)
            author = cells[2].get_text(strip=True) if len(cells) > 2 else ""
            date = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            items.append({
                "ntt_id": ntt_id,
                "post_number": post_number,
                "title": title,
                "author": author,
                "date": date,
                "detail_url": (
                    f"{_DETAIL_URL}?nttId={ntt_id}&bbsId={_BBS_ID}&mn={_MN}"
                ),
            })
        return items

    def _get_max_page(self, html: str) -> int:
        """Detect maximum pageIndex from pagination links."""
        pages = [int(m) for m in re.findall(r'pageIndex=(\d+)', html)]
        return max(pages) if pages else 1

    def _parse_detail(self, html: str) -> dict:
        """Parse a detail page, returning metadata+abstract+attachment dict."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] detail-page parse error: {exc}")
            return {}

        # Abstract -------------------------------------------------------
        content_div = soup.find("div", class_="board-view-content")
        abstract = content_div.get_text(" ", strip=True) if content_div else ""

        # Metadata from board-view-top -----------------------------------
        detail_title = detail_date = detail_author = detail_phone = ""
        top = soup.find("div", class_="board-view-top")
        if top:
            title_tag = top.find("strong", class_="board-view-title")
            if title_tag:
                detail_title = title_tag.get_text(strip=True)
            for li in top.find_all("li"):
                b_tag = li.find("b")
                span_tag = li.find("span")
                if not (b_tag and span_tag):
                    continue
                key = b_tag.get_text(strip=True)
                val = span_tag.get_text(strip=True)
                if "작성일" in key:
                    detail_date = val
                elif "작성자" in key:
                    detail_author = val
                elif "전화번호" in key:
                    detail_phone = val

        # File attachment ------------------------------------------------
        pdf_url = None
        original_filename = None
        file_div = (top.find("div", class_="file-container") if top else None) \
            or soup.find("div", class_="file-container")

        if file_div:
            # Filename: look for span containing a file extension
            for span in file_div.find_all("span"):
                txt = span.get_text(strip=True)
                if txt and re.search(r'\.\w{2,5}$', txt) and "[" not in txt:
                    original_filename = txt
                    break

            # First PDF download link (strip jsessionid)
            for a in file_div.find_all("a"):
                href = a.get("href", "")
                if "BoardFileDown.do" in href:
                    clean = _strip_jsessionid(href)
                    if not clean.startswith("http"):
                        clean = _BASE_URL + clean
                    pdf_url = clean
                    break

        return {
            "detail_title": detail_title,
            "detail_date": detail_date,
            "detail_author": detail_author,
            "detail_phone": detail_phone,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")

        # Fetch page 1 to detect pagination depth
        p1_html = self._curl(f"{_LIST_URL}?bbsId={_BBS_ID}&mn={_MN}&pageIndex=1")
        if not p1_html:
            print(f"[{_SITE_ID}] Failed to fetch page 1; aborting")
            return 0

        max_page = min(self._get_max_page(p1_html), _MAX_PAGES)
        print(f"[{_SITE_ID}] Detected {max_page} pages total")

        page_cache: dict[int, str] = {1: p1_html}

        for p in range(1, max_page + 1):
            if saved >= limit_or_inf:
                break
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{_SITE_ID}] Wall-clock budget reached at page {p}; exiting")
                break
            if p == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety page cap ({_MAX_PAGES}) reached; exiting")
                break
            if p % 10 == 0:
                print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_or_inf}")

            # Load page HTML (page 1 already cached)
            if p in page_cache:
                list_html = page_cache[p]
            else:
                url = f"{_LIST_URL}?bbsId={_BBS_ID}&mn={_MN}&pageIndex={p}"
                list_html = self._curl(url)
                if not list_html:
                    print(f"[{_SITE_ID}] page {p}: fetch failed, skipping")
                    continue

            items = self._parse_list_page(list_html)
            if not items:
                print(f"[{_SITE_ID}] page {p}: no items found; stopping pagination")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > _MAX_WALL_SECONDS:
                    break

                detail_url = item["detail_url"]
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_html = self._curl(detail_url)
                    if not detail_html:
                        print(
                            f"[{_SITE_ID}] nttId={item['ntt_id']}: "
                            "detail fetch failed, skipping"
                        )
                        continue

                    detail = self._parse_detail(detail_html)

                    abstract = detail.get("abstract", "")
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] nttId={item['ntt_id']}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    title = detail.get("detail_title") or item["title"]
                    date_str = detail.get("detail_date") or item["date"]
                    author = detail.get("detail_author") or item["author"]

                    pub_date = (
                        date_str
                        if re.match(r'\d{4}-\d{2}-\d{2}', date_str or "")
                        else None
                    )

                    metadata: dict = {
                        "nttId": item["ntt_id"],
                        "bbsId": _BBS_ID,
                        "posted_date": date_str or None,
                    }
                    if detail.get("detail_phone"):
                        metadata["phone"] = detail["detail_phone"]
                    if detail.get("original_filename"):
                        metadata["originalFilename"] = detail["original_filename"]

                    paper = {
                        "site_id": _SITE_ID,
                        "external_id": item["ntt_id"],
                        "post_number": item["post_number"],
                        "title": title,
                        "abstract": abstract,
                        "authors": author,
                        "publisher": "국가유산청",
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "published_date": pub_date,
                        "posted_date": pub_date,
                        "listed_date": pub_date,
                        "original_filename": detail.get("original_filename"),
                        "category": "통계정보",
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{_SITE_ID}] nttId={item.get('ntt_id', '?')}: "
                        f"failed: {exc}"
                    )
                    continue

            if new_on_page == 0:
                print(
                    f"[{_SITE_ID}] page {p}: all items already seen; "
                    "stopping pagination"
                )
                break

        print(f"[{_SITE_ID}] done: saved {saved} items total")
        return saved
