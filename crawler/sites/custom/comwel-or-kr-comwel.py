# -*- coding: utf-8 -*-
"""Korea Workers' Compensation & Welfare Service (COMWEL) press releases crawler.

Starting URL: https://www.comwel.or.kr/comwel/noti/pres.jsp
List/detail pages are a common gov board template (board_no=892) — list rows
give 번호(post number)/title/article_no/작성자/첨부/등록일, detail pages give the
full article body plus attachment links.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import parse_qs, urljoin, urlparse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


_DATE_RE = re.compile(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})")


def _normalize_date(text: str) -> str:
    if not text:
        return ""
    m = _DATE_RE.search(text)
    if not m:
        return ""
    y, mo, d = m.groups()
    return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"


class ComwelOrKrComwelCrawler(BaseCrawler):
    """Crawler for COMWEL (comwel.or.kr) press releases board."""

    site_id = "comwel-or-kr-comwel"
    site_name = "Custom: comwel-or-kr-comwel"
    base_url = "https://www.comwel.or.kr"

    _LIST_PATH = "/comwel/noti/pres.jsp"
    _BOARD_NO = "892"
    _PAGE_SIZE = 10
    _MIN_ABSTRACT = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:  # noqa: BLE001
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _list_url(self, offset: int) -> str:
        return (
            f"{self.base_url}{self._LIST_PATH}?mode=list"
            f"&board_no={self._BOARD_NO}&pager.offset={offset}"
        )

    def _detail_url(self, article_no: str) -> str:
        return (
            f"{self.base_url}{self._LIST_PATH}?mode=view&article_no={article_no}"
            f"&board_wrapper=%2Fcomwel%2Fnoti%2Fpres.jsp&pager.offset=0"
            f"&board_no={self._BOARD_NO}"
        )

    def _parse_list_page(self, raw_html: str) -> list:
        """Return a list of dicts: post_number, article_no, title, url, department, listed_date."""
        entries = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.site_id}] list HTML parse error: {exc}")
            return entries

        table = soup.find("table", class_="list_table")
        if table is None:
            return entries

        header_labels = []
        thead = table.find("thead")
        if thead:
            header_labels = [th.get_text(strip=True) for th in thead.find_all("th")]

        tbody = table.find("tbody")
        if tbody is None:
            return entries

        for row in tbody.find_all("tr"):
            try:
                tds = row.find_all("td")
                if not tds:
                    continue

                link = row.find("a", href=re.compile(r"mode=view"))
                if not link:
                    continue
                href = link.get("href") or ""
                qs = parse_qs(urlparse(href).query)
                article_no = (qs.get("article_no") or [""])[0]
                if not article_no:
                    continue

                title = link.get_text(strip=True)
                if not title:
                    continue

                col_map = {}
                if header_labels and len(header_labels) == len(tds):
                    col_map = {label: idx for idx, label in enumerate(header_labels)}

                def _cell(label, fallback_idx):
                    idx = col_map.get(label)
                    if idx is None:
                        idx = fallback_idx
                    if idx is not None and 0 <= idx < len(tds):
                        return tds[idx].get_text(strip=True)
                    return ""

                post_number = _cell("번호", 0)
                department = _cell("작성자", 2)
                listed_date_raw = _cell("등록일", len(tds) - 2)

                entries.append({
                    "post_number": post_number,
                    "article_no": article_no,
                    "title": title,
                    "url": self._detail_url(article_no),
                    "department": department,
                    "listed_date_raw": listed_date_raw,
                    "listed_date": _normalize_date(listed_date_raw),
                })
            except Exception as exc:  # noqa: BLE001
                print(f"[{self.site_id}] list row parse error: {exc}")
                continue

        return entries

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail_page(self, raw_html: str) -> dict:
        """Return dict with abstract/body text, attachment url/filename, dates."""
        result = {
            "abstract": "",
            "pdf_url": None,
            "original_filename": None,
            "published_date": "",
        }
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.site_id}] detail HTML parse error: {exc}")
            return result

        article_div = soup.find("div", id="article_text")
        if article_div is not None:
            text = article_div.get_text("\n", strip=True).replace("\xa0", " ")
            text = re.sub(r"[ \t]+", " ", text)
            result["abstract"] = re.sub(r"\n{2,}", "\n", text).strip()

        # 등록일 shown in the view table (작성자/등록일/조회수 row)
        for th in soup.find_all("th"):
            if "등록일" in th.get_text(strip=True):
                td = th.find_next_sibling("td")
                if td:
                    result["published_date"] = _normalize_date(td.get_text(strip=True))
                break

        attach_link = soup.find("a", class_="down-btn", href=re.compile(r"download\.jsp"))
        if attach_link is None:
            attach_link = soup.find("a", href=re.compile(r"download\.jsp\?attach_no="))
        if attach_link is not None:
            href = attach_link.get("href") or ""
            result["pdf_url"] = urljoin(self.base_url, href)

            span_txt = attach_link.find("span", class_="txt")
            filename = span_txt.get_text(strip=True) if span_txt else ""
            if not filename:
                title_attr = attach_link.get("title") or ""
                filename = re.sub(r"\s*다운로드\s*$", "", title_attr).strip()
            result["original_filename"] = filename or None

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl COMWEL press releases page by page (newest first)."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page_num = 0
        offset = 0
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_num += 1
                raw = self._curl_get(self._list_url(offset))
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page at offset {offset}. Stopping.")
                    break

                entries = self._parse_list_page(raw)
                if not entries:
                    print(f"[{self.site_id}] No entries at offset {offset}. Reached end of pagination.")
                    break

                new_on_page = 0
                for entry in entries:
                    if limit is not None and saved >= limit:
                        break

                    url = entry["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    try:
                        time.sleep(self._delay)
                        detail_raw = self._curl_get(url)
                        if not detail_raw:
                            print(f"[{self.site_id}] Failed to fetch detail for {entry['article_no']}. Skipping.")
                            continue

                        detail = self._parse_detail_page(detail_raw)
                        abstract = detail["abstract"]
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] Short abstract ({len(abstract)} chars) for "
                                f"'{entry['title'][:50]}', skipping."
                            )
                            continue

                        listed_date = entry["listed_date"]
                        published_date = detail["published_date"] or listed_date

                        meta = {
                            "posted_date": entry["listed_date_raw"],
                            "originalFilename": detail["original_filename"],
                            "board_no": self._BOARD_NO,
                            "article_no": entry["article_no"],
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": entry["article_no"],
                            "post_number": entry["post_number"] or entry["article_no"],
                            "title": entry["title"],
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": None,
                            "publisher": "근로복지공단",
                            "department": entry["department"] or None,
                            "journal": None,
                            "url": url,
                            "pdf_url": detail["pdf_url"],
                            "keywords": None,
                            "category": None,
                            "doi": None,
                            "original_filename": detail["original_filename"],
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {entry['title'][:60]}")

                    except Exception as exc:  # noqa: BLE001
                        print(
                            f"[{self.site_id}] item failed "
                            f"(article_no={entry.get('article_no', '?')}): {exc}; continuing."
                        )
                        continue

                if new_on_page == 0:
                    print(f"[{self.site_id}] Page at offset {offset} contained only already-seen items. Stopping.")
                    break

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

                offset += self._PAGE_SIZE

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
