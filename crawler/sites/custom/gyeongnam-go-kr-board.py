# -*- coding: utf-8 -*-
"""Crawler for 경상남도청 보도해명자료 board (BBS_0000060)."""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_SITE_ID = "gyeongnam-go-kr-board"
_BASE_URL = "https://www.gyeongnam.go.kr"
_LIST_URL = _BASE_URL + "/board/list.gyeong"
_DETAIL_URL = _BASE_URL + "/board/view.gyeong"
_BOARD_ID = "BBS_0000060"
_MENU_CD = "DOM_000000135002001000"
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 50
_WALL_CLOCK_BUDGET_SECONDS = 25 * 60


class GyeongnamGoKrBoardCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: gyeongnam-go-kr-board"
    base_url = _BASE_URL

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self._detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, referer=None, timeout=45):
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_error = "unknown error"
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} stderr={stderr}"

            if attempt < len(waits):
                print(
                    f"[{_SITE_ID}] curl attempt {attempt}/{len(waits)} failed for {url}: "
                    f"{last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page_no):
        params = {
            "boardId": _BOARD_ID,
            "menuCd": _MENU_CD,
            "paging": "ok",
            "categoryCode1": "A",
            "searchType": "",
            "keyword": "",
            "pageNo": page_no,
        }
        url = f"{_LIST_URL}?{urlencode(params)}"
        return self._curl(url, referer=_BASE_URL + "/")

    def _fetch_detail(self, data_sid):
        params = {
            "boardId": _BOARD_ID,
            "menuCd": _MENU_CD,
            "paging": "ok",
            "startPage": 1,
            "searchType": "",
            "searchOperation": "AND",
            "keyword": "",
            "categoryCode1": "A",
            "dataSid": data_sid,
        }
        url = f"{_DETAIL_URL}?{urlencode(params)}"
        return self._curl(url, referer=_LIST_URL)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _one_line(value) -> str:
        if value is None:
            return ""
        return re.sub(r"\s+", " ", str(value)).strip()

    @staticmethod
    def _to_iso_date(raw):
        raw = (raw or "").strip()
        if not raw:
            return None
        m = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})$", raw)
        if m:
            yy, mm, dd = m.groups()
            return f"{2000 + int(yy):04d}-{mm}-{dd}"
        m = re.match(r"^(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})$", raw)
        if m:
            y, mo, d = m.groups()
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            return []

        table = soup.select_one("table.basicList")
        if table is None:
            return []

        items = []
        for tr in table.select("tbody tr"):
            try:
                title_td = tr.select_one("td.title")
                if title_td is None:
                    continue
                a = title_td.select_one("a[href*='view.gyeong']")
                if a is None or not a.get("href"):
                    continue

                href = a["href"]
                m = re.search(r"dataSid=(\d+)", href)
                if not m:
                    continue
                data_sid = m.group(1)

                title = self._one_line(a.get_text(" ", strip=True))
                if not title:
                    continue

                num_td = tr.select_one("td.num")
                date_td = tr.select_one("td.date")
                tds = tr.find_all("td")
                department = self._one_line(tds[4].get_text(" ", strip=True)) if len(tds) >= 5 else ""

                items.append({
                    "data_sid": data_sid,
                    "title": title,
                    "post_number": self._one_line(num_td.get_text(strip=True)) if num_td else None,
                    "listed_date_raw": self._one_line(date_td.get_text(strip=True)) if date_td else None,
                    "department": department or None,
                    "url": urljoin(_BASE_URL, href),
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list row parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, raw):
        soup = self._parse_html(raw)
        if soup is None:
            raise ValueError("all HTML parsers failed on detail page")

        fields = {}
        attachments = []
        con_field = soup.select_one(".conField")
        if con_field is not None:
            for li in con_field.select("li"):
                span = li.select_one("span")
                p = li.select_one("p")
                if span is None or p is None:
                    continue
                label = self._one_line(span.get_text(strip=True))
                if label == "첨부파일":
                    for link in p.select("a.file[href]"):
                        href = link.get("href", "").strip()
                        if not href:
                            continue
                        fname = self._one_line(link.get_text(strip=True))
                        fname = re.sub(r"\s*\(\d+\s*kb\)\s*$", "", fname, flags=re.I).strip()
                        attachments.append({
                            "url": urljoin(_BASE_URL, href),
                            "filename": fname,
                        })
                else:
                    fields[label] = self._one_line(p.get_text(" ", strip=True))

        abstract = ""
        con_text = soup.select_one(".conText")
        if con_text is not None:
            abstract = self._one_line(con_text.get_text(" ", strip=True))

        published_raw = None
        title_field = soup.select_one(".titleField")
        if title_field is not None:
            for li in title_field.select("li"):
                text = li.get_text(" ", strip=True)
                if "등록일" in text:
                    m = re.search(r"(\d{2}\.\d{2}\.\d{2})", text)
                    if m:
                        published_raw = m.group(1)

        return {
            "fields": fields,
            "attachments": attachments,
            "abstract": abstract,
            "published_raw": published_raw,
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        limit_label = str(limit) if limit is not None else "inf"
        crawl_start = time.time()

        page = 1
        while True:
            elapsed = time.time() - crawl_start
            if elapsed > _WALL_CLOCK_BUDGET_SECONDS:
                print(f"[{_SITE_ID}] {_WALL_CLOCK_BUDGET_SECONDS}s wall-clock budget reached; stopping at page {page}")
                break

            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            try:
                raw_list = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] failed to fetch list page {page}: {exc}")
                break

            if not raw_list:
                print(f"[{_SITE_ID}] empty list response at page {page}; stopping")
                break

            items = self._parse_list_page(raw_list)
            if not items:
                print(f"[{_SITE_ID}] no rows on page {page}; stopping")
                break

            new_urls = [it["url"] for it in items if it["url"] not in seen_urls]
            if not new_urls:
                print(f"[{_SITE_ID}] all URLs on page {page} already seen; stopping (paginator looped)")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item["url"]
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                data_sid = item["data_sid"]

                try:
                    time.sleep(self._detail_delay)
                    raw_detail = self._fetch_detail(data_sid)
                    if not raw_detail:
                        print(f"[{_SITE_ID}] item {data_sid} failed: empty detail response")
                        continue

                    detail = self._parse_detail(raw_detail)
                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{_SITE_ID}] item {data_sid} skipped: abstract too short ({len(abstract)} chars)")
                        continue

                    fields = detail.get("fields", {})
                    department = fields.get("제공부서") or item.get("department")
                    category = fields.get("구분")
                    subtitle = fields.get("부제목")

                    listed_iso = self._to_iso_date(item.get("listed_date_raw"))
                    published_iso = self._to_iso_date(detail.get("published_raw")) or listed_iso

                    attachments = detail.get("attachments") or []
                    pdf_url = attachments[0]["url"] if attachments else None
                    original_filename = attachments[0]["filename"] if attachments else None

                    metadata = {
                        "posted_date": item.get("listed_date_raw"),
                        "originalFilename": original_filename,
                        "dataSid": data_sid,
                        "boardId": _BOARD_ID,
                        "menuCd": _MENU_CD,
                        "department": department,
                        "subtitle": subtitle,
                        "manager": fields.get("담당자"),
                        "phone": fields.get("전화번호"),
                        "attachments": attachments,
                    }

                    paper = {
                        "id": str(uuid.uuid4()),
                        "site_id": _SITE_ID,
                        "external_id": data_sid,
                        "post_number": item.get("post_number"),
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": published_iso,
                        "posted_date": listed_iso,
                        "authors": None,
                        "publisher": "경상남도",
                        "department": department,
                        "journal": None,
                        "url": url,
                        "pdf_url": pdf_url,
                        "keywords": None,
                        "category": category,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {data_sid} failed: {exc}")
                    continue

            page += 1

        print(f"[{_SITE_ID}] crawl complete: saved {saved}/{limit_label}")
        return saved
