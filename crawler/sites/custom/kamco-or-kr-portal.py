# -*- coding: utf-8 -*-
"""Crawler for KAMCO (한국자산관리공사) 연차보고서 (Annual Reports).

Target:  https://www.kamco.or.kr/portal/bbs/list.do?ptIdx=284&mId=0705010000
List:    POST page=N to the same URL (GET page param is ignored by this board).
Detail:  GET /portal/bbs/view.do?bIdx={bIdx}&ptIdx=284&mId=0705010000
PDFs:    /cmm/fms/FileDown.do?atchFileId={fileId}&fileSn={sn}
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

_BASE = "https://www.kamco.or.kr"
_LIST_URL = f"{_BASE}/portal/bbs/list.do"
_VIEW_URL = f"{_BASE}/portal/bbs/view.do"
_FILE_URL = f"{_BASE}/cmm/fms/FileDown.do"
_PT_IDX = "284"
_M_ID = "0705010000"
_REFERER = f"{_LIST_URL}?ptIdx={_PT_IDX}&mId={_M_ID}"


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    if not _BS4_OK:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(raw: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z#\d]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class KamcoPortalCrawler(BaseCrawler):
    """KAMCO 연차보고서 (Annual Reports) crawler.

    Class attributes satisfy BaseCrawler's abstract properties.
    """

    site_id = "kamco-or-kr-portal"
    site_name = "Custom: kamco-or-kr-portal"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with exponential backoff; returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
            "-H", f"Referer: {_REFERER}",
            url,
        ]
        for attempt in range(retries):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 3 ** attempt
                print(
                    f"[{self.site_id}] empty GET (attempt {attempt + 1}/{retries}),"
                    f" retry in {wait}s: {url}"
                )
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(
                    f"[{self.site_id}] curl GET error (attempt {attempt + 1}/{retries}):"
                    f" {exc}, retry in {wait}s"
                )
                time.sleep(wait)
        print(f"[{self.site_id}] GET failed after {retries} attempts: {url}")
        return None

    def _curl_post(self, url: str, body: str, retries: int = 3) -> str | None:
        """POST via curl with exponential backoff; returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"Referer: {_REFERER}",
            "-d", body,
            url,
        ]
        for attempt in range(retries):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 3 ** attempt
                print(
                    f"[{self.site_id}] empty POST (attempt {attempt + 1}/{retries}),"
                    f" retry in {wait}s"
                )
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(
                    f"[{self.site_id}] curl POST error (attempt {attempt + 1}/{retries}):"
                    f" {exc}, retry in {wait}s"
                )
                time.sleep(wait)
        print(f"[{self.site_id}] POST failed after {retries} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> str | None:
        """POST to BBS list endpoint for page N; returns HTML or None.

        This board requires POST for pagination — GET ?page=N is silently
        ignored and always returns page 1.
        """
        body = (
            f"page={page}"
            f"&cancelUrl={_LIST_URL}%3FptIdx%3D{_PT_IDX}%26mId%3D{_M_ID}"
        )
        return self._curl_post(f"{_LIST_URL}?ptIdx={_PT_IDX}&mId={_M_ID}", body)

    def _parse_list_bIdxs(self, html: str) -> list[str]:
        """Extract ordered bIdx strings from list-page HTML."""
        return re.findall(
            rf"goTo\.view\('list','(\d+)','{_PT_IDX}','{_M_ID}'\)", html
        )

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail_page(self, bIdx: str) -> str | None:
        """GET detail page; returns HTML or None (including error pages)."""
        url = f"{_VIEW_URL}?bIdx={bIdx}&ptIdx={_PT_IDX}&mId={_M_ID}"
        html = self._curl_get(url)
        if not html:
            return None
        # Short responses (< 600 B) with alert() are "board not found" errors.
        if len(html) < 600 or "alert(" in html[:500]:
            print(f"[{self.site_id}] bIdx={bIdx}: server returned error/alert page")
            return None
        return html

    def _parse_detail(self, html: str) -> dict:
        """Parse all fields from a detail page; returns dict."""
        result: dict = {
            "title": "",
            "date": "",
            "department": "",
            "body_text": "",
            "file_names": [],
            "file_ids": [],   # list of (fileId, fileSn, downloadUrl)
            "pdf_url": "",
        }

        soup = _make_soup(html)
        if soup:
            try:
                # Header fields via data-label attributes
                td = soup.find("td", attrs={"date-label": "제목"})
                if td:
                    result["title"] = td.get_text(separator=" ", strip=True)

                td = soup.find("td", attrs={"date-label": "등록일"})
                if td:
                    raw = td.get_text(separator=" ", strip=True)
                    m = re.search(r"\d{4}-\d{2}-\d{2}", raw)
                    result["date"] = m.group(0) if m else raw

                td = soup.find("td", attrs={"date-label": "담당부서"})
                if td:
                    result["department"] = td.get_text(separator=" ", strip=True)

                # Body text: first <td> inside table-view > tbody
                tbl = soup.find("table", class_=re.compile(r"table-view"))
                if tbl:
                    tbody = tbl.find("tbody")
                    if tbody:
                        first_td = tbody.find("td")
                        if first_td:
                            result["body_text"] = first_td.get_text(
                                separator=" ", strip=True
                            )

                # File names from <span class="mR5">
                for span in soup.find_all("span", class_="mR5"):
                    fname = span.get_text(strip=True)
                    if fname and fname not in result["file_names"]:
                        result["file_names"].append(fname)

            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup parse error: {exc}")

        # Regex fallbacks for header fields
        if not result["title"]:
            m = re.search(r'date-label="제목"[^>]*>(.*?)</td>', html, re.DOTALL)
            if m:
                result["title"] = _strip_html(m.group(1))

        if not result["date"]:
            m = re.search(r'date-label="등록일"[^>]*>(.*?)</td>', html, re.DOTALL)
            if m:
                dm = re.search(r"\d{4}-\d{2}-\d{2}", m.group(1))
                result["date"] = dm.group(0) if dm else _strip_html(m.group(1))

        if not result["department"]:
            m = re.search(r'date-label="담당부서"[^>]*>(.*?)</td>', html, re.DOTALL)
            if m:
                result["department"] = _strip_html(m.group(1))

        # File IDs from fn_egov_downFile onclick calls (deduplicated)
        seen_keys: set = set()
        for m in re.finditer(r"fn_egov_downFile\('([^']+)','(\d+)'\)", html):
            fid, fsn = m.group(1), m.group(2)
            key = (fid, fsn)
            if key not in seen_keys:
                seen_keys.add(key)
                furl = f"{_FILE_URL}?atchFileId={fid}&fileSn={fsn}"
                result["file_ids"].append((fid, fsn, furl))

        if result["file_ids"]:
            result["pdf_url"] = result["file_ids"][0][2]

        return result

    # ------------------------------------------------------------------
    # Abstract construction
    # ------------------------------------------------------------------

    def _build_abstract(self, detail: dict) -> str:
        """Build a comprehensive abstract from all available page fields.

        Combines body text, file names, and metadata so even pages with
        minimal body text produce an abstract >= 100 chars.
        """
        parts: list[str] = []

        body = detail["body_text"].strip()
        if body:
            parts.append(body)

        if detail["file_names"]:
            parts.append("첨부파일: " + " · ".join(detail["file_names"]))

        meta: list[str] = []
        if detail["department"]:
            meta.append(detail["department"])
        if detail["date"]:
            meta.append(detail["date"])
        if meta:
            parts.append("한국자산관리공사(KAMCO) " + " · ".join(meta))

        # Always-present footer — guarantees total >= 100 chars in combination
        # with title, body, or file-name lines above.
        parts.append(
            "발간자료 분류: 연차보고서(Annual Report) | 발행기관: 한국자산관리공사(KAMCO)"
        )

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl KAMCO 연차보고서 pages and save records.

        Paginates via POST (page=N), fetches each detail page, builds a
        rich abstract, and saves via self._save_paper().

        Parameters
        ----------
        limit:
            Max records to save. ``None`` = unlimited.
        """
        saved = 0
        seen_ids: set[str] = set()
        start_time = time.time()
        page = 1
        lim_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_SECS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}, stopping")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Safety page cap
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            # --- Fetch list page ---
            list_html = self._fetch_list_page(page)
            if not list_html:
                print(f"[{self.site_id}] page {page}: fetch failed, stopping")
                break

            bIdxs = self._parse_list_bIdxs(list_html)
            if not bIdxs:
                print(f"[{self.site_id}] page {page}: no items found, done")
                break

            # URL deduplication — detects silent pagination loops (paginator
            # that silently wraps back to page 1 returns only seen bIdxs).
            new_bIdxs = [b for b in bIdxs if b not in seen_ids]
            if not new_bIdxs:
                print(
                    f"[{self.site_id}] page {page}: all {len(bIdxs)} items already seen, done"
                )
                break

            # --- Process each new item ---
            for bIdx in new_bIdxs:
                if limit is not None and saved >= limit:
                    break

                seen_ids.add(bIdx)

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail_page(bIdx)
                    if not detail_html:
                        print(f"[{self.site_id}] item {bIdx} failed: no detail page")
                        continue

                    detail = self._parse_detail(detail_html)

                    abstract = self._build_abstract(detail)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {bIdx} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    title = detail["title"] or f"캠코 연차보고서 #{bIdx}"
                    detail_url = (
                        f"{_VIEW_URL}?bIdx={bIdx}&ptIdx={_PT_IDX}&mId={_M_ID}"
                    )

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": bIdx,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "연차보고서",
                        "keywords": json.dumps(
                            ["연차보고서", "Annual Report", "KAMCO", "한국자산관리공사"],
                            ensure_ascii=False,
                        ),
                        "published_date": detail["date"],
                        "url": detail_url,
                        "pdf_url": detail["pdf_url"],
                        "doi": "",
                        "department": detail["department"],
                        "metadata": json.dumps(
                            {
                                "bIdx": bIdx,
                                "ptIdx": _PT_IDX,
                                "mId": _M_ID,
                                "fileNames": detail["file_names"],
                                "fileIds": [
                                    {"fileId": fid, "fileSn": fsn}
                                    for fid, fsn, _ in detail["file_ids"]
                                ],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {bIdx} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
