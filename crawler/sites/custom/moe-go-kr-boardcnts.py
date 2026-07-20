# -*- coding: utf-8 -*-
"""Crawler for 교육부 감사정보 게시판 (moe.go.kr boardID=345)."""

import json
import re
import subprocess
import sys
import os
import time

# Absolute import so spec_from_file_location works without package context.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BOARD_ID = "345"
_M = "041202"
_S = "moe"
_LIST_URL = "https://www.moe.go.kr/boardCnts/listRenew.do"
_VIEW_BASE = "https://www.moe.go.kr/boardCnts/viewRenew.do"
_BASE = "https://www.moe.go.kr"
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60


def _bs4(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No BS4 parser available")


def _strip_html(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url: str) -> str | None:
    """GET via curl (TLS 1.3, -sk) with 3-attempt exponential backoff."""
    delays = [1, 3, 9]
    for attempt in range(3):
        try:
            result = subprocess.run(
                ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                capture_output=True, timeout=35,
            )
            if result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
            if attempt < 2:
                print(f"[moe-go-kr-boardcnts] Empty response, retry in {delays[attempt]}s…")
                time.sleep(delays[attempt])
        except Exception as exc:
            if attempt < 2:
                print(f"[moe-go-kr-boardcnts] curl error: {exc}, retry in {delays[attempt]}s…")
                time.sleep(delays[attempt])
            else:
                print(f"[moe-go-kr-boardcnts] curl failed after 3 attempts: {exc}")
    return None


class MoeGoKrBoardCntsCrawler(BaseCrawler):
    """교육부 사전정보공표 게시판 (boardID=346) crawler."""

    site_id = "moe-go-kr-boardcnts"
    site_name = "Custom: moe-go-kr-boardcnts"
    base_url = "https://www.moe.go.kr"

    # ------------------------------------------------------------------

    def _list_url(self, page: int) -> str:
        return (
            f"{_LIST_URL}?type=default&page={page}"
            f"&m={_M}&s={_S}&boardID={_BOARD_ID}"
        )

    def _detail_url(self, board_seq: str, page: int = 1) -> str:
        return (
            f"{_VIEW_BASE}?boardID={_BOARD_ID}&boardSeq={board_seq}"
            f"&lev=0&searchType=null&statusYN=W&page={page}"
            f"&s={_S}&m={_M}&opType=N"
        )

    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> list[dict]:
        """Return list of {board_seq, title, dept, date, post_number} dicts."""
        items = []
        try:
            soup = _bs4(html)
        except Exception as exc:
            print(f"[moe-go-kr-boardcnts] BS4 init failed on list page: {exc}")
            # Fallback: regex extraction
            calls = re.findall(
                r"goView\('345',\s*'(\d+)',\s*'[^']*',\s*[^,]+,\s*'[^']*',\s*'(\d+)'",
                html,
            )
            dates = re.findall(r"\d{4}-\d{2}-\d{2}", html)
            for i, (bseq, _page) in enumerate(calls):
                items.append({
                    "board_seq": bseq,
                    "title": "",
                    "dept": "",
                    "date": dates[i] if i < len(dates) else "",
                    "post_number": None,
                })
            return items

        try:
            # There are multiple <tbody> elements in the page (nav tables, etc.).
            # Search all <tr> elements globally and filter by the board-list pattern:
            # a row must have both <td class="no"> and <td class="title">.
            for tr in soup.find_all("tr"):
                no_td = tr.find("td", class_="no")
                title_td = tr.find("td", class_="title")
                if not no_td or not title_td:
                    continue

                a_tag = title_td.find("a")
                if not a_tag:
                    continue
                title = a_tag.get("title") or a_tag.get_text(strip=True)
                onclick = a_tag.get("onclick", "")
                bm = re.search(r"goView\('345',\s*'(\d+)'", onclick)
                if not bm:
                    continue
                board_seq = bm.group(1)
                post_number = no_td.get_text(strip=True)

                tds = tr.find_all("td")
                dept = tds[2].get_text(strip=True) if len(tds) > 2 else ""
                date_str = tds[3].get_text(strip=True) if len(tds) > 3 else ""

                items.append({
                    "board_seq": board_seq,
                    "title": title,
                    "dept": dept,
                    "date": date_str,
                    "post_number": post_number,
                })
        except Exception as exc:
            print(f"[moe-go-kr-boardcnts] List parse error: {exc}")

        return items

    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, board_seq: str) -> dict:
        """Parse detail page HTML into a result dict."""
        result: dict = {}
        try:
            soup = _bs4(html)
        except Exception as exc:
            print(f"[moe-go-kr-boardcnts] BS4 init failed on detail {board_seq}: {exc}")
            return result

        try:
            # Title
            th_title = soup.find("th", string=re.compile(r"제목"))
            if th_title:
                td = th_title.find_next_sibling("td")
                if td:
                    result["title"] = td.get_text(strip=True)

            # Date
            th_date = soup.find("th", string=re.compile(r"등록일"))
            if th_date:
                td = th_date.find_next_sibling("td")
                if td:
                    result["date"] = td.get_text(strip=True)

            # Department (grab first line to avoid phone number noise)
            th_dept = soup.find("th", string=re.compile(r"담당부서"))
            if th_dept:
                td = th_dept.find_next_sibling("td")
                if td:
                    raw = td.get_text("\n", strip=True)
                    result["dept"] = raw.split("\n")[0].strip()
                    result["dept_phone"] = raw.split("\n")[1].strip() if "\n" in raw else ""

            # Attached files
            files: list[dict] = []
            th_atch = soup.find("th", string=re.compile(r"첨부파일"))
            if th_atch:
                td = th_atch.find_next_sibling("td")
                if td:
                    for li in td.find_all("li"):
                        dl_a = li.find("a", href=re.compile(r"fileDown"))
                        if not dl_a:
                            continue
                        href = dl_a.get("href", "")
                        full_url = f"{_BASE}{href}" if href.startswith("/") else href
                        # Filename: text of li before [ size ]
                        raw_text = li.get_text(" ", strip=True)
                        fname_m = re.match(r"^(.+?)\s*\[", raw_text)
                        fname = fname_m.group(1).strip() if fname_m else raw_text
                        files.append({"filename": fname, "url": full_url})
            result["files"] = files

            # Body content
            body_div = soup.find("div", attrs={"data-content": True})
            if body_div:
                result["body"] = _strip_html(str(body_div))
            else:
                result["body"] = ""

        except Exception as exc:
            print(f"[moe-go-kr-boardcnts] Detail parse error for {board_seq}: {exc}")

        return result

    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 교육부 사전정보공표 (boardID=346)."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget
            if time.time() - start_time > _CRAWL_BUDGET_SECS:
                print(f"[moe-go-kr-boardcnts] 25-minute budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[moe-go-kr-boardcnts] page {page}: saved {saved}/{limit_display}")

            # Fetch list
            list_raw = _curl_get(self._list_url(page))
            if not list_raw:
                print(f"[moe-go-kr-boardcnts] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list(list_raw)
            if not items:
                print(f"[moe-go-kr-boardcnts] No items on page {page}. Done.")
                break

            new_this_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                board_seq = item["board_seq"]
                det_url = self._detail_url(board_seq, page=page)

                if det_url in seen_urls:
                    continue
                seen_urls.add(det_url)
                new_this_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(det_url)
                    if not detail_raw:
                        print(f"[moe-go-kr-boardcnts] item {board_seq} fetch failed, skipping")
                        continue

                    detail = self._parse_detail(detail_raw, board_seq)

                    title = detail.get("title") or item.get("title", "")
                    body = detail.get("body", "").strip()
                    dept = detail.get("dept") or item.get("dept", "")
                    dept_phone = detail.get("dept_phone", "")
                    date_str = detail.get("date") or item.get("date", "")
                    files = detail.get("files", [])

                    # Build abstract: title + body + file names + dept + date
                    parts: list[str] = []
                    if title:
                        parts.append(title)
                    if body:
                        parts.append(body)
                    if files:
                        fnames = [f["filename"] for f in files if f.get("filename")]
                        if fnames:
                            parts.append("첨부파일: " + "; ".join(fnames))
                    dept_line = dept
                    if dept_phone:
                        dept_line = f"{dept} {dept_phone}"
                    if dept_line:
                        parts.append(f"담당부서: {dept_line}")
                    if date_str:
                        parts.append(f"등록일: {date_str}")
                    # Ensure board context is always present for minimum length
                    parts.append(f"게시판: 교육부 감사정보 (boardID={_BOARD_ID}) 담당: {dept} 등록일: {date_str}")

                    abstract = "\n".join(parts)

                    if len(abstract) < 100:
                        print(
                            f"[moe-go-kr-boardcnts] Skipping {board_seq}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # PDF: prefer .pdf extension, else first file
                    pdf_url = None
                    original_filename = None
                    for f in files:
                        fname = f.get("filename", "")
                        furl = f.get("url", "")
                        if fname.lower().endswith(".pdf") or ".pdf" in furl.lower():
                            pdf_url = furl
                            original_filename = fname
                            break
                    if pdf_url is None and files:
                        pdf_url = files[0].get("url")
                        original_filename = files[0].get("filename")

                    post_number = item.get("post_number") or board_seq

                    meta = {
                        "posted_date": date_str,
                        "nttId": board_seq,
                        "boardID": _BOARD_ID,
                        "dept": dept,
                        "dept_phone": dept_phone,
                        "files": [
                            {"filename": f["filename"], "url": f["url"]}
                            for f in files
                        ],
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": board_seq,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "listed_date": date_str,
                        "url": det_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "department": dept,
                        "publisher": "교육부",
                        "authors": None,
                        "keywords": None,
                        "category": "감사정보",
                        "doi": None,
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[moe-go-kr-boardcnts] Saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[moe-go-kr-boardcnts] item {board_seq} failed: {exc}")
                    continue

            if new_this_page == 0:
                print(f"[moe-go-kr-boardcnts] All items on page {page} already seen. Done.")
                break

        if page >= _MAX_PAGES:
            print(f"[moe-go-kr-boardcnts] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[moe-go-kr-boardcnts] Done. Total saved: {saved}")
        return saved
