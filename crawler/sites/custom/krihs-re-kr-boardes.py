# -*- coding: utf-8 -*-
"""Crawler for KRIHS (Korea Research Institute for Human Settlements) press releases.

Board URL: https://www.krihs.re.kr/board.es?mid=a10607000000&bid=0008
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

_SITE_ID = "krihs-re-kr-boardes"
_BASE_URL = "https://www.krihs.re.kr"
_LIST_URL = "https://www.krihs.re.kr/board.es?mid=a10607000000&bid=0008"
_DETAIL_URL = "https://www.krihs.re.kr/board.es?mid=a10607000000&bid=0008&act=view&list_no={list_no}&tag=&nPage=1"
_DOWNLOAD_URL = "https://www.krihs.re.kr/boardDownload.es?bid=0008&list_no={list_no}&seq={seq}"
_PAGE_SIZE = 10
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 50


def _try_bs4(raw: str):
    """Construct BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags, decode entities, and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _curl_get(url: str, user_agent: str, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3 and retry logic."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "-L",
        "--max-time", "30",
        "-H", f"User-Agent: {user_agent}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] Empty response for {url}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error ({exc}), retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts for {url}: {exc}")
    return None


def _parse_list_page(html: str) -> list[dict]:
    """Parse list page HTML; return list of {list_no, title, listed_date, department, category}."""
    soup = _try_bs4(html)
    if soup is None:
        return []
    rows = []
    tbody = soup.find("table", class_="tstyle_list")
    if tbody is None:
        return []
    for tr in tbody.find_all("tr"):
        a_tag = tr.find("a", href=re.compile(r"list_no=\d+"))
        if not a_tag:
            continue
        m = re.search(r"list_no=(\d+)", a_tag["href"])
        if not m:
            continue
        list_no = m.group(1)
        title = _strip_tags(str(a_tag))

        td_date = tr.find("td", attrs={"aria-label": re.compile(r"등록일|보도일|날짜")})
        listed_date = td_date.get_text(strip=True) if td_date else ""

        td_dept = tr.find("td", attrs={"aria-label": "작성자"})
        department = td_dept.get_text(strip=True) if td_dept else ""

        td_cate = tr.find("td", attrs={"aria-label": "분류"})
        category = td_cate.get_text(strip=True) if td_cate else ""

        rows.append({
            "list_no": list_no,
            "title": title,
            "listed_date": listed_date,
            "department": department,
            "category": category,
        })
    return rows


def _parse_detail_page(html: str, list_no: str) -> dict | None:
    """Parse detail page; return dict with all extracted fields."""
    soup = _try_bs4(html)
    if soup is None:
        return None

    view = soup.find(class_="board_view")
    if view is None:
        return None

    # Title
    h2 = view.find("h2", class_="title")
    title = h2.get_text(strip=True) if h2 else ""

    # Info fields: 작성일, 분류
    published_date = ""
    category = ""
    info_ul = view.find("ul", class_="info")
    if info_ul:
        for li in info_ul.find_all("li"):
            strong = li.find("strong")
            span = li.find("span")
            if strong and span:
                label = strong.get_text(strip=True)
                val = span.get_text(strip=True)
                if "작성일" in label or "등록일" in label:
                    published_date = val
                elif "분류" in label:
                    category = val

    # Abstract: strip tags from div.contents
    contents_div = view.find("div", class_="contents")
    abstract = ""
    if contents_div:
        abstract = _strip_tags(str(contents_div))

    # Attachments: find file list section
    pdf_url = None
    original_filename = None
    attachments = []

    file_section = view.find("div", class_=re.compile(r"file|attach"))
    if file_section is None:
        # Fall back: look for boardDownload links anywhere on page
        file_section = soup

    dl_links = file_section.find_all("a", href=re.compile(r"/boardDownload\.es"))
    for a in dl_links:
        href = a.get("href", "")
        seq_m = re.search(r"seq=(\d+)", href)
        if not seq_m:
            continue
        seq = seq_m.group(1)
        full_url = urljoin(_BASE_URL, href)

        # Find the filename text near this link — it's a sibling text node in the <li>
        li_parent = a.find_parent("li")
        filename = ""
        if li_parent:
            raw_text = li_parent.get_text(separator=" ")
            # Pick the segment that looks like a filename (has extension)
            for seg in raw_text.split():
                if re.search(r"\.\w{2,5}$", seg):
                    filename = seg.strip()
                    break

        attachments.append({
            "seq": seq,
            "url": full_url,
            "filename": filename,
        })

        # Choose the first PDF as the canonical pdf_url
        if pdf_url is None and filename.lower().endswith(".pdf"):
            pdf_url = full_url
            original_filename = filename

    # If no PDF found by extension, check img alt for "pdf 첨부파일"
    if pdf_url is None:
        for li in (view if view else soup).find_all("li"):
            img = li.find("img", alt=re.compile(r"pdf", re.I))
            if img:
                dl_a = li.find("a", href=re.compile(r"/boardDownload\.es"))
                if dl_a:
                    href = dl_a.get("href", "")
                    pdf_url = urljoin(_BASE_URL, href)
                    # Extract filename from text
                    raw_text = li.get_text(separator=" ")
                    for seg in raw_text.split():
                        if re.search(r"\.\w{2,5}$", seg):
                            original_filename = seg.strip()
                            break
                    break

    return {
        "title": title,
        "published_date": published_date,
        "category": category,
        "abstract": abstract,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
        "attachments": attachments,
    }


class KrihsReKrBoardesCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: krihs-re-kr-boardes"
    base_url = _BASE_URL

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        max_wall_seconds = 25 * 60  # 25-minute budget

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget
            if time.monotonic() - start_time > max_wall_seconds:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached at page {page}. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            list_url = f"{_LIST_URL}&nPage={page}"
            raw = _curl_get(list_url, self.USER_AGENT)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            items = _parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] No items on list page {page}. Done.")
                break

            # Detect looping: if all list_nos were already seen, we've looped
            new_items = [it for it in items if it["list_no"] not in seen_urls]
            if not new_items:
                print(f"[{_SITE_ID}] All items on page {page} already seen. Done.")
                break

            for it in items:
                seen_urls.add(it["list_no"])

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            for item in items:
                if limit is not None and saved >= limit:
                    break

                list_no = item["list_no"]
                detail_url = _DETAIL_URL.format(list_no=list_no)

                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(detail_url, self.USER_AGENT)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] item {list_no}: detail fetch failed, skipping")
                        continue

                    detail = _parse_detail_page(detail_raw, list_no)
                    if not detail:
                        print(f"[{_SITE_ID}] item {list_no}: detail parse failed, skipping")
                        continue

                    title = detail["title"] or item["title"]
                    abstract = detail["abstract"]

                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(f"[{_SITE_ID}] item {list_no}: abstract too short ({len(abstract)} chars), skipping")
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": list_no,
                        "url": detail_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": detail["published_date"] or item["listed_date"],
                        "posted_date": item["listed_date"],
                        "publisher": "국토연구원",
                        "department": item["department"],
                        "category": detail["category"] or item["category"],
                        "pdf_url": detail["pdf_url"],
                        "original_filename": detail["original_filename"],
                        "metadata": json.dumps({
                            "posted_date": item["listed_date"],
                            "list_no": list_no,
                            "attachments": detail["attachments"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {list_no} failed: {exc}")
                    continue

            # Detect last page: check if page has fewer items than page size
            # or if all page numbers are exhausted
            if len(items) < _PAGE_SIZE and len(new_items) < _PAGE_SIZE:
                print(f"[{_SITE_ID}] Page {page} had {len(items)} items (< {_PAGE_SIZE}). Likely last page.")
                break

        if page >= _MAX_PAGES:
            print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
