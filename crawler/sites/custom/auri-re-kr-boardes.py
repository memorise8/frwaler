# -*- coding: utf-8 -*-
"""건축공간연구원 보도자료 board crawler.

Target: https://www.auri.re.kr/board.es?mid=a10401030000&bid=0013
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.auri.re.kr"
_MID = "a10401030000"
_BID = "0013"
_PUBLISHER = "건축공간연구원"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_ABSTRACT_MIN_CHARS = 100


# ---------------------------------------------------------------------------
# HTML fetch helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, max_retries: int = 3) -> str | None:
    """GET via curl with retries. Returns decoded text or None."""
    delays = [1, 3, 9]
    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                [
                    "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
                    "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
                    "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                    url,
                ],
                capture_output=True,
                timeout=35,
            )
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[auri-re-kr-boardes] curl error (attempt {attempt + 1}/{max_retries}): {exc}")
        if attempt < max_retries - 1:
            time.sleep(delays[attempt])
    return None


def _make_soup(html: str):
    """Build BeautifulSoup with parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _strip_html(html_text: str) -> str:
    """Strip tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> str:
    """Convert '2026/04/16' or '2026/04/16 19:43' → '2026-04-16'. Returns '' on failure."""
    if not raw:
        return ""
    m = re.match(r"(\d{4})[/\-](\d{2})[/\-](\d{2})", raw.strip())
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else ""


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class AuriReKrBoardesCrawler(BaseCrawler):
    site_id = "auri-re-kr-boardes"
    site_name = "Custom: auri-re-kr-boardes"
    base_url = "https://www.auri.re.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page = 1
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock budget exceeded ({_MAX_WALL_SECONDS}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            list_url = f"{_BASE}/board.es?mid={_MID}&bid={_BID}&nPage={page}"
            raw = _curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] HTML parse error on list page {page}: {exc}. Stopping.")
                break

            table = soup.find("table", class_="tstyle_list")
            if not table:
                print(f"[{self.site_id}] No list table on page {page}. Done.")
                break

            data_rows = [r for r in table.find_all("tr") if r.find("td")]
            if not data_rows:
                print(f"[{self.site_id}] No data rows on page {page}. Done.")
                break

            new_on_page = 0

            for row in data_rows:
                if limit is not None and saved >= limit:
                    break

                try:
                    item_saved = self._process_row(row, seen_urls, saved, limit_str)
                    if item_saved is None:
                        continue
                    saved += item_saved
                    new_on_page += item_saved
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] row processing failed: {exc}")
                    continue

                time.sleep(self._delay)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            if new_on_page == 0:
                print(f"[{self.site_id}] No new URLs on page {page}. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-row processing (isolated)
    # ------------------------------------------------------------------

    def _process_row(self, row, seen_urls: set, saved: int, limit_str: str) -> int | None:
        """Process one list row. Returns 1 if saved, None to skip."""
        title_td = row.find("td", class_="txt_left")
        if not title_td:
            return None

        a_tag = title_td.find("a")
        if not a_tag:
            return None

        href = a_tag.get("href", "")
        if not href:
            return None

        detail_url = urljoin(_BASE, href)
        if detail_url in seen_urls:
            return None
        seen_urls.add(detail_url)

        # Native post ID (list_no)
        list_no_m = re.search(r"list_no=(\d+)", href)
        list_no = list_no_m.group(1) if list_no_m else None

        # board 번호 (display sequence number)
        board_num = None
        # listed_date from list page
        listed_date = ""
        # attachment info from list
        list_pdf_url = None
        list_orig_filename = None

        for cell in row.find_all("td"):
            label = cell.get("aria-label", "")
            if label == "번호":
                txt = cell.get_text(strip=True)
                if txt.isdigit():
                    board_num = txt
            elif label == "등록일":
                listed_date = _parse_date(cell.get_text(strip=True))
            elif label == "첨부파일":
                for al in cell.find_all("a"):
                    ah = al.get("href", "")
                    if not ah:
                        continue
                    img = al.find("img")
                    alt = img.get("alt", "") if img else al.get_text(strip=True)
                    full_url = urljoin(_BASE, ah)
                    if alt.lower().endswith(".pdf"):
                        list_pdf_url = full_url
                        list_orig_filename = alt
                        break
                    if not list_orig_filename and alt:
                        list_orig_filename = alt
                        list_pdf_url = full_url

        # Fetch detail page
        detail = self._fetch_detail(detail_url, list_no)
        if detail is None:
            print(f"[{self.site_id}] item {list_no} detail fetch returned None, skipping")
            return None

        abstract = detail.get("abstract", "")
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(f"[{self.site_id}] item {list_no} abstract too short "
                  f"({len(abstract)} chars < {_ABSTRACT_MIN_CHARS}), skipping")
            return None

        title = detail.get("title") or a_tag.get_text(strip=True).strip(" \"'")
        published_date = detail.get("published_date") or listed_date
        pdf_url = detail.get("pdf_url") or list_pdf_url
        orig_filename = detail.get("original_filename") or list_orig_filename
        attachments = detail.get("attachments", [])

        paper = {
            "site_id": self.site_id,
            "external_id": list_no,
            "post_number": list_no,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": "",
            "publisher": _PUBLISHER,
            "department": "",
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url or "",
            "keywords": "",
            "category": "보도자료",
            "doi": "",
            "original_filename": orig_filename or "",
            "metadata": json.dumps({
                "posted_date": listed_date,
                "originalFilename": orig_filename or "",
                "list_no": list_no,
                "board_number": board_num,
                "attachments": attachments,
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
        counter = f"{saved + 1}/{limit_str}"
        print(f"[{self.site_id}] saved {counter}: {title[:60]}")
        return 1

    # ------------------------------------------------------------------
    # Detail page fetch
    # ------------------------------------------------------------------

    def _fetch_detail(self, url: str, list_no: str | None) -> dict | None:
        """Fetch and parse a board detail page."""
        raw = _curl_get(url)
        if not raw:
            print(f"[{self.site_id}] item {list_no} fetch failed after retries")
            return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error {list_no}: {exc}")
            return None

        # Scope everything to the board_view article to avoid nav false matches
        board_view = (soup.find("article", class_="board_view")
                      or soup.find("div", class_="board_view")
                      or soup)

        # Title
        h2 = board_view.find("h2", class_="title")
        title = h2.get_text(strip=True) if h2 else ""

        # Published date (작성일 from detail)
        published_date = ""
        date_li = board_view.find("li", class_="date")
        if date_li:
            span = date_li.find("span")
            if span:
                published_date = _parse_date(span.get_text(strip=True))

        # Abstract from contents div
        abstract = ""
        contents_div = board_view.find("div", class_="contents")
        if contents_div:
            abstract = _strip_html(str(contents_div))

        # Attachments
        attachments = []
        pdf_url = None
        orig_filename = None

        for a in soup.find_all("a", href=re.compile(r"boardDownload\.es", re.I)):
            ah = a.get("href", "")
            full_url = urljoin(_BASE, ah)
            img = a.find("img")
            if img:
                filename = img.get("alt", "").strip()
            else:
                filename = a.get_text(strip=True)
            attachments.append({"url": full_url, "filename": filename})
            if filename.lower().endswith(".pdf") and not pdf_url:
                pdf_url = full_url
                orig_filename = filename
            elif not orig_filename and filename:
                orig_filename = filename

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "pdf_url": pdf_url,
            "original_filename": orig_filename,
            "attachments": attachments,
        }
