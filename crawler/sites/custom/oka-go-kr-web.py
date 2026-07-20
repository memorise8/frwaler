# -*- coding: utf-8 -*-
"""Crawler for oka.go.kr (재외동포청) 법령자료실 board.

Starting URL: https://www.oka.go.kr/web/board/brdList.do?menu_cd=000164

The site is a JSP/JSON CMS (eGovFrame-style). The board endpoints return
JSON directly once ``Accept: application/json`` is sent explicitly:

- list:   /web/board/ajax/list.do?menu_cd=000164&currentPage=N&searchData=contdata&searchText=
- detail: /web/board/brdDetail.do?menu_cd=000164&num=<num>
- file:   /web/board/fileDownload/<file_num>.do

Each board item's ``cont`` field is a short HTML snippet (often just the
title plus a legal citation, since the substantive content of this
"법령자료실" board lives in the attached PDF/HWP files, not in the board
body). The abstract is built from the stripped ``cont`` text plus
genuinely-available structured metadata (department, contact, category,
attachment filenames) rather than being padded with artificial filler.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from typing import Any, Optional

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


SITE_ID = "oka-go-kr-web"


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _strip_html(raw: Optional[str]) -> str:
    """Strip HTML tags from a content fragment, defensively.

    Fallback chain: html5lib -> lxml -> html.parser. Never raises.
    """
    if not raw:
        return ""
    from bs4 import BeautifulSoup

    last_error = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            soup = BeautifulSoup(raw, parser)
            return _clean_text(soup.get_text(" ", strip=True))
        except Exception as exc:
            last_error = exc
            print(f"[{SITE_ID}] BeautifulSoup({parser}) failed on cont fragment: {exc}")
            continue
    print(f"[{SITE_ID}] all HTML parsers failed on cont fragment: {last_error}")
    return ""


def _iso_date(raw: Any) -> Optional[str]:
    if not raw:
        return None
    text = _clean_text(raw)
    if not text:
        return None
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if match:
        return match.group(1)
    match = re.search(r"\b(\d{4})[./](\d{1,2})[./](\d{1,2})\b", text)
    if match:
        y, m, d = match.groups()
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return None


def _json_dumps(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class OkaGoKrWebCrawler(BaseCrawler):
    """Crawler for oka.go.kr (재외동포청) 법령자료실 board."""

    site_id = "oka-go-kr-web"
    site_name = "Custom: oka-go-kr-web"
    base_url = "https://www.oka.go.kr"

    MENU_CD = "000164"
    _SAFETY_CAP = 200
    _WALL_BUDGET_SECONDS = 25 * 60
    _MIN_ABSTRACT_CHARS = 50
    _BACKOFF_SECONDS = (1, 3, 9)

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else "inf"

        for page in range(1, self._SAFETY_CAP + 1):
            if time.time() - start_time > self._WALL_BUDGET_SECONDS:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; stopping")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_data = self._fetch_list_page(page)
            if not list_data:
                print(f"[{self.site_id}] page {page}: fetch failed or empty; stopping")
                break

            items = list_data.get("brdList") or []
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                num = item.get("num")
                if num is None:
                    continue
                item_url = self._detail_url(num)
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    paper = self._build_paper(item, item_url)
                    if paper is None:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipping '{paper.get('title', '')[:70]}' "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no unseen records; stopping")
                break

            total_pages = (list_data.get("pagingInfoVO") or {}).get("totalPageCount")
            if total_pages and page >= int(total_pages):
                print(f"[{self.site_id}] reached last page ({total_pages}); stopping")
                break
        else:
            print(f"[{self.site_id}] safety cap of {self._SAFETY_CAP} pages reached")

        print(f"[{self.site_id}] done: saved {saved}")
        return saved

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, label: str = "", timeout: int = 30) -> Optional[str]:
        cmd = [
            "curl",
            "--tls-max",
            "1.3",
            "-skL",
            "--connect-timeout",
            "15",
            "--max-time",
            str(timeout),
            "-A",
            self.USER_AGENT,
            "-H",
            "Accept: application/json",
            "-H",
            "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            url,
        ]

        for attempt, wait in enumerate(self._BACKOFF_SECONDS, start=1):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=timeout + 10,
                    check=False,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body

                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                reason = stderr or f"curl exit {result.returncode}, body length {len(body)}"
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] {label or url} failed after 3 attempts: {reason}")
                    return None
                print(f"[{self.site_id}] {label or url} attempt {attempt}/3 failed: {reason}; retrying in {wait}s")
                time.sleep(wait)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                if attempt == len(self._BACKOFF_SECONDS):
                    print(f"[{self.site_id}] {label or url} failed after 3 attempts: {exc}")
                    return None
                print(f"[{self.site_id}] {label or url} attempt {attempt}/3 failed: {exc}; retrying in {wait}s")
                time.sleep(wait)
        return None

    def _fetch_list_page(self, page: int) -> Optional[dict]:
        url = (
            f"{self.base_url}/web/board/ajax/list.do?menu_cd={self.MENU_CD}"
            f"&currentPage={page}&searchData=contdata&searchText="
        )
        raw = self._curl_get(url, label=f"list page {page}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] list page {page}: JSON parse failed: {exc}")
            return None

    def _fetch_detail(self, num) -> Optional[dict]:
        url = f"{self.base_url}/web/board/brdDetail.do?menu_cd={self.MENU_CD}&num={num}"
        raw = self._curl_get(url, label=f"detail num={num}")
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception as exc:
            print(f"[{self.site_id}] detail num={num}: JSON parse failed: {exc}")
            return None

    def _detail_url(self, num) -> str:
        return f"{self.base_url}/web/board/brdDetail.do?menu_cd={self.MENU_CD}&num={num}"

    # ------------------------------------------------------------------
    # Parsing / building
    # ------------------------------------------------------------------

    def _build_paper(self, list_item: dict, item_url: str) -> Optional[dict]:
        num = list_item.get("num")
        detail = self._fetch_detail(num)
        if detail is None:
            print(f"[{self.site_id}] item {item_url} failed: detail fetch/parse failed")
            return None

        brd = detail.get("brd") or {}
        menu_info = detail.get("menuInfo") or {}
        site_vo = detail.get("siteVo") or {}
        file_list = detail.get("fileList") or []

        title = _clean_text(brd.get("title") or list_item.get("title")) or "(untitled)"

        cont_html = brd.get("cont") or list_item.get("cont")
        cont_text = _strip_html(cont_html)

        department = menu_info.get("depart")
        depart_number = menu_info.get("depart_number")
        menu_nm = menu_info.get("menu_nm")
        publisher = site_vo.get("site_nm") or "재외동포청"

        attachments = [
            _clean_text(f.get("file_org"))
            for f in file_list
            if isinstance(f, dict) and f.get("file_org")
        ]

        abstract = self._build_abstract(cont_text, department, depart_number, menu_nm, attachments)

        published_raw = list_item.get("write_dt") or brd.get("write_dt") or brd.get("disp_write_dt")
        listed_raw = brd.get("disp_write_dt") or list_item.get("disp_write_dt") or published_raw
        published_date = _iso_date(published_raw)
        listed_date = _iso_date(listed_raw)

        pdf_entry = None
        for f in file_list:
            if not isinstance(f, dict):
                continue
            ct = (f.get("contenttype") or "").lower()
            fname = (f.get("file_org") or "").lower()
            if "pdf" in ct or fname.endswith(".pdf"):
                pdf_entry = f
                break
        if pdf_entry is None and file_list and isinstance(file_list[0], dict):
            pdf_entry = file_list[0]

        pdf_url = None
        original_filename = None
        if pdf_entry is not None and pdf_entry.get("num"):
            pdf_url = f"{self.base_url}/web/board/fileDownload/{pdf_entry['num']}.do"
            original_filename = _clean_text(pdf_entry.get("file_org")) or None

        external_id = str(num) if num is not None else None
        post_number = str(num) if num is not None else None

        metadata = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "num": num,
            "menu_id": menu_info.get("menu_id") or brd.get("menu_id"),
            "bbsSeq": num,
            "menu_cd": self.MENU_CD,
            "depart": department,
            "depart_number": depart_number,
            "attachments": attachments,
            "state": brd.get("state"),
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": None,
            "publisher": publisher,
            "department": department,
            "journal": None,
            "url": item_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": menu_nm,
            "doi": None,
            "original_filename": original_filename,
            "metadata": _json_dumps(metadata),
        }

    def _build_abstract(
        self,
        cont_text: str,
        department: Optional[str],
        depart_number: Optional[str],
        menu_nm: Optional[str],
        attachments: list[str],
    ) -> str:
        parts = []
        if cont_text:
            parts.append(cont_text)
        if department:
            parts.append(f"소관부서: {department}")
        if depart_number:
            parts.append(f"연락처: {depart_number}")
        if menu_nm:
            parts.append(f"분류: {menu_nm}")
        if attachments:
            parts.append("첨부파일: " + "; ".join(attachments))
        return " ".join(p for p in parts if p)
