# -*- coding: utf-8 -*-
"""국가인권위원회 통계 게시판 crawler (boardManagementNo=20)."""

import json
import re
import subprocess
import time
from urllib.parse import urlencode, urlparse, parse_qs

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.humanrights.go.kr"
_LIST_URL = f"{_BASE}/base/board/list"
_READ_URL = f"{_BASE}/base/board/read"
_BOARD_MGMT_NO = "20"
_MENU_LEVEL = "3"
_MENU_NO = "120"
_PAGE_SIZE = 10  # items per page observed
_SAFETY_CAP = 200  # max pages

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def _bs(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, params: dict | None = None, max_retries: int = 3) -> str | None:
    """GET via curl with TLS workaround and exponential backoff."""
    if params:
        url = url + "?" + urlencode(params)
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-A", _UA,
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
        url,
    ]
    for attempt in range(max_retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
            if attempt < max_retries - 1:
                wait = 3 ** attempt
                time.sleep(wait)
        except Exception as exc:
            if attempt < max_retries - 1:
                wait = 3 ** attempt
                print(f"[humanrights-go-kr-base] curl error: {exc}, retry in {wait}s")
                time.sleep(wait)
            else:
                print(f"[humanrights-go-kr-base] curl failed after {max_retries} attempts: {exc}")
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_list_page(html: str) -> list[dict]:
    """Parse one board list page; return list of item dicts."""
    soup = _bs(html)
    if soup is None:
        return []
    items = []
    tbody = soup.find("tbody")
    if not tbody:
        return items
    for tr in tbody.find_all("tr"):
        try:
            num_td = tr.find("td", class_="num")
            sort_td = tr.find("td", class_="sort")
            tit_td = tr.find("td", class_="tit")
            date_td = tr.find("td", class_="date")

            if not tit_td:
                continue

            a_tag = tit_td.find("a", class_="td-title")
            if not a_tag:
                continue

            title = a_tag.get_text(" ", strip=True)
            href = a_tag.get("href", "")

            # Extract boardNo from href
            m = re.search(r"boardNo=(\d+)", href)
            if not m:
                continue
            board_no = m.group(1)

            seq_num = num_td.get_text(strip=True) if num_td else ""
            category = sort_td.get_text(strip=True) if sort_td else ""
            listed_date = date_td.get_text(strip=True) if date_td else ""

            # Normalise href to canonical URL (strip port 443 from :443)
            detail_url = (
                f"{_BASE}/base/board/read"
                f"?boardManagementNo={_BOARD_MGMT_NO}"
                f"&boardNo={board_no}"
                f"&page=1"
                f"&menuLevel={_MENU_LEVEL}"
                f"&menuNo={_MENU_NO}"
            )

            items.append({
                "board_no": board_no,
                "seq_num": seq_num,
                "title": title,
                "category": category,
                "listed_date": listed_date,
                "url": detail_url,
            })
        except Exception as exc:
            print(f"[humanrights-go-kr-base] list row parse error: {exc}")
            continue
    return items


def _parse_detail_page(html: str, board_no: str) -> dict:
    """Parse a detail page; return enriched item dict."""
    soup = _bs(html)
    result: dict = {
        "department": "",
        "published_date": "",
        "content_text": "",
        "file_links": [],
        "file_names": [],
    }
    if soup is None:
        return result

    try:
        view_div = soup.find("div", class_="board_view")
        if not view_div:
            return result

        # Department and date from span.each elements
        for span in view_div.find_all("span", class_="each"):
            text = span.get_text(strip=True)
            if "담당부서" in text:
                result["department"] = text.replace("담당부서 :", "").strip()
            elif "등록일" in text:
                raw_date = text.replace("등록일 :", "").strip()
                # Normalise to YYYY-MM-DD
                m = re.search(r"(\d{4}-\d{2}-\d{2})", raw_date)
                if m:
                    result["published_date"] = m.group(1)

        # Content from editor_view
        editor = view_div.find("div", class_="editor_view")
        if editor:
            result["content_text"] = _strip_tags(str(editor))

        # Attached files
        file_box = view_div.find("div", class_="file_box")
        if file_box:
            for p in file_box.find_all("p", class_="file_each"):
                a_tags = p.find_all("a")
                for a in a_tags:
                    href = a.get("href", "")
                    text = a.get_text(strip=True)
                    if "download" in href.lower() or "BASIC_ATTACH" in href:
                        # Make absolute if needed
                        if href.startswith("/"):
                            href = _BASE + href
                        # Strip :443
                        href = href.replace(":443", "")
                        result["file_links"].append(href)
                        # Try to get filename from title attr first
                        fname = a.get("title", "")
                        if fname:
                            fname = re.sub(r"\s*다운로드$", "", fname).strip()
                        if not fname:
                            fname = text
                        if fname:
                            result["file_names"].append(fname)
    except Exception as exc:
        print(f"[humanrights-go-kr-base] detail parse error ({board_no}): {exc}")

    return result


def _build_abstract(title: str, category: str, dept: str,
                    date: str, content: str, file_names: list[str]) -> str:
    """Build a composite abstract from all available fields."""
    parts = []
    if content:
        parts.append(content)
    # Always include structured metadata so abstract is rich enough
    parts.append(f"제목: {title}")
    if category:
        parts.append(f"구분: {category}")
    if dept:
        parts.append(f"담당부서: {dept}")
    if date:
        parts.append(f"등록일: {date}")
    if file_names:
        parts.append(f"첨부파일: {', '.join(file_names)}")
    parts.append("출처: 국가인권위원회")
    return "\n".join(parts)


class HumanrightsGoKrBaseCrawler(BaseCrawler):
    """국가인권위원회 통계 게시판 (boardManagementNo=20) crawler."""

    site_id = "humanrights-go-kr-base"
    site_name = "Custom: humanrights-go-kr-base"
    base_url = "https://www.humanrights.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minute budget

        try:
            for page in range(1, _SAFETY_CAP + 1):
                # Time budget check
                if time.time() - start_time > max_seconds:
                    print(f"[humanrights-go-kr-base] 25-minute budget reached at page {page}. Stopping.")
                    break

                if limit is not None and saved >= limit:
                    break

                if page == _SAFETY_CAP:
                    print(f"[humanrights-go-kr-base] Safety cap of {_SAFETY_CAP} pages reached.")

                # Fetch list page
                params = {
                    "boardManagementNo": _BOARD_MGMT_NO,
                    "menuLevel": _MENU_LEVEL,
                    "menuNo": _MENU_NO,
                    "page": str(page),
                }
                html = _curl_get(_LIST_URL, params)
                if not html:
                    print(f"[humanrights-go-kr-base] Failed to fetch list page {page}. Stopping.")
                    break

                items = _parse_list_page(html)
                if not items:
                    print(f"[humanrights-go-kr-base] No items on page {page}. Done.")
                    break

                # Dedup check: if all items on this page already seen → stop
                new_items = [it for it in items if it["url"] not in seen_urls]
                if not new_items:
                    print(f"[humanrights-go-kr-base] All items on page {page} already seen. Done.")
                    break

                for item in items:
                    if limit is not None and saved >= limit:
                        break
                    if item["url"] in seen_urls:
                        continue
                    seen_urls.add(item["url"])

                    board_no = item["board_no"]
                    title = item["title"]
                    category = item["category"]
                    listed_date = item["listed_date"]
                    detail_url = item["url"]

                    try:
                        time.sleep(self._delay)
                        detail_html = _curl_get(detail_url)
                        if not detail_html:
                            print(f"[humanrights-go-kr-base] Failed to fetch detail {board_no}. Skipping.")
                            continue

                        det = _parse_detail_page(detail_html, board_no)

                        pub_date = det["published_date"] or listed_date
                        dept = det["department"]
                        file_links = det["file_links"]
                        file_names = det["file_names"]
                        content_text = det["content_text"]

                        abstract = _build_abstract(
                            title, category, dept,
                            pub_date, content_text, file_names
                        )

                        if len(abstract) < 50:
                            print(f"[humanrights-go-kr-base] Abstract too short (<50 chars) for {board_no}: {title[:40]}. Skipping.")
                            continue

                        # Pick best PDF/HWP url
                        pdf_url = ""
                        original_filename = ""
                        for link, fname in zip(file_links, file_names):
                            if link.lower().endswith(".pdf") or ".pdf" in link.lower():
                                pdf_url = link
                                original_filename = fname
                                break
                        if not pdf_url and file_links:
                            pdf_url = file_links[0]
                            original_filename = file_names[0] if file_names else ""

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": board_no,
                            "post_number": board_no,
                            "title": title,
                            "abstract": abstract,
                            "published_date": pub_date,
                            "listed_date": listed_date,
                            "authors": "",
                            "publisher": "국가인권위원회",
                            "department": dept,
                            "journal": "",
                            "url": detail_url,
                            "pdf_url": pdf_url,
                            "keywords": "",
                            "category": category,
                            "doi": "",
                            "original_filename": original_filename,
                            "metadata": json.dumps({
                                "posted_date": listed_date,
                                "originalFilename": original_filename,
                                "boardManagementNo": _BOARD_MGMT_NO,
                                "boardNo": board_no,
                                "seqNum": item["seq_num"],
                                "fileLinks": file_links,
                                "fileNames": file_names,
                                "menuNo": _MENU_NO,
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        limit_str = str(limit) if limit is not None else "∞"
                        print(f"[humanrights-go-kr-base] Saved {saved}/{limit_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[humanrights-go-kr-base] Item {board_no} failed: {exc}")
                        continue

                if page % 10 == 0:
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[humanrights-go-kr-base] page {page}: saved {saved}/{limit_str}")

                time.sleep(0.5)

        except KeyboardInterrupt:
            print(f"[humanrights-go-kr-base] Interrupted by user at saved={saved}.")
            raise

        print(f"[humanrights-go-kr-base] Done. Total saved: {saved}")
        return saved
