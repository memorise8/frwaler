# -*- coding: utf-8 -*-
"""방송미디어통신위원회(kmcc.go.kr) 보도자료 crawler — boardId=1113 (user.do).

Starting URL:
https://www.kmcc.go.kr/user.do?boardId=1113&page=A05030000&dc=K05030000
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from html import unescape
from pathlib import Path
from urllib.parse import urlencode

# Ensure the project root is on sys.path so the crawler package is importable
# when this module is loaded via spec_from_file_location (no package context).
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

_BASE = "https://www.kmcc.go.kr"
_LIST_URL = _BASE + "/user.do"
_BOARD_ID = "1113"
_PAGE = "A05030000"
_DC = "K05030000"
_LIST_PARAMS_BASE = {"boardId": _BOARD_ID, "page": _PAGE, "dc": _DC}
_PAGE_SIZE = 30
_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60  # 25 minutes
_RATE_LIMIT = 1.0  # seconds between detail fetches
_SKIP_ABSTRACT_UNDER = 100  # skip items whose abstract is shorter than this


class KmccUserDoCrawler(BaseCrawler):
    site_id = "kmcc-go-kr-userdo"
    site_name = "Custom: kmcc-go-kr-userdo"
    base_url = "https://www.kmcc.go.kr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 30) -> str | None:
        """Fetch URL via curl with TLS 1.3 and retries; returns decoded text or None."""
        cmd = [
            "curl",
            "--tls-max", "1.3",
            "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
                body = result.stdout
                if body:
                    try:
                        return body.decode("utf-8")
                    except UnicodeDecodeError:
                        return body.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[{self.site_id}] Empty response for {url}, retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}, retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {exc}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html: str):
        """Parse HTML with parser fallback chain: html5lib -> lxml -> html.parser."""
        if not _BS4_OK:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _strip_tags(text: str) -> str:
        text = re.sub(r"<[^>]+>", " ", text)
        text = text.replace("&nbsp;", " ")
        text = unescape(text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _clean_href(href: str) -> str:
        """Remove ;jsessionid=... path segment from a URL."""
        return re.sub(r";jsessionid=[^?&/]*", "", href or "")

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html: str) -> list:
        """Parse the board listing HTML; return list of item dicts."""
        soup = self._make_soup(html)
        items = []
        if not soup:
            print(f"[{self.site_id}] Could not build soup for list page; skipping page.")
            return items

        table = soup.find("table", class_=re.compile(r"\btable_style01\b"))
        if not table:
            return items
        tbody = table.find("tbody") or table

        for tr in tbody.find_all("tr"):
            try:
                tds = tr.find_all("td")
                if len(tds) < 6:
                    continue
                num_text = tds[0].get_text(strip=True)
                if not num_text or not num_text.isdigit():
                    continue
                post_number = num_text

                link_tag = tds[1].find("a", href=True)
                if not link_tag:
                    continue
                title = re.sub(r"\s+", " ", link_tag.get_text()).strip()
                href = self._clean_href(link_tag.get("href", ""))
                seq_m = re.search(r"boardSeq=(\d+)", href)
                if not seq_m:
                    continue
                board_seq = seq_m.group(1)
                detail_url = (
                    f"{_BASE}/user.do?mode=view&page={_PAGE}&dc={_DC}"
                    f"&boardId={_BOARD_ID}&boardSeq={board_seq}"
                )

                department = tds[2].get_text(strip=True) if len(tds) > 2 else ""
                license_type = tds[3].get_text(strip=True) if len(tds) > 3 else ""

                file_links = []
                if len(tds) > 4:
                    for a in tds[4].find_all("a", href=True):
                        a_href = self._clean_href(a.get("href", ""))
                        if "/download.do" not in a_href:
                            continue
                        full_url = _BASE + a_href if a_href.startswith("/") else a_href
                        img = a.find("img")
                        filename = img.get("alt", "").strip() if img else ""
                        file_links.append({"url": full_url, "name": filename})

                listed_date = tds[5].get_text(strip=True) if len(tds) > 5 else ""

                items.append({
                    "post_number": post_number,
                    "board_seq": board_seq,
                    "title": title,
                    "detail_url": detail_url,
                    "department": department,
                    "license_type": license_type,
                    "file_links": file_links,
                    "listed_date": listed_date,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list row parse error: {exc}; skipping row.")
                continue
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str) -> dict:
        """Parse detail page; return dict with title/content/author/date/files."""
        result: dict = {"files": []}
        soup = self._make_soup(html)
        if not soup:
            return result

        detail_table = soup.find("table", summary=re.compile(r"제목.*담당부서"))
        if not detail_table:
            for t in soup.find_all("table"):
                cap = t.find("caption")
                if cap and "상세" in cap.get_text():
                    detail_table = t
                    break
        if not detail_table:
            return result

        title_td = detail_table.find("td", class_="table_title")
        if title_td:
            result["title"] = title_td.get_text(strip=True)

        content_td = detail_table.find("td", class_="table_con")
        if content_td:
            result["content"] = content_td.get_text(separator="\n", strip=True)

        for th in detail_table.find_all("th"):
            th_text = th.get_text(strip=True)
            td = th.find_next_sibling("td")
            if not td:
                continue
            td_text = td.get_text(strip=True)
            if "담당부서" in th_text:
                result["department"] = td_text
            elif "작성자" in th_text:
                result["author"] = td_text
            elif "등록일" in th_text:
                result["registered_date"] = td_text
            elif "연락처" in th_text:
                result["contact"] = td_text
            elif "공공누리" in th_text:
                result["license_type"] = td_text
            elif "첨부파일" in th_text:
                files = []
                for a in td.find_all("a", href=True):
                    a_href = self._clean_href(a.get("href", ""))
                    if "/download.do" not in a_href:
                        continue
                    span = a.find("span")
                    if not span:
                        # Skip the duplicate plain "다운로드" link that carries
                        # the same fileSeq but no filename span.
                        continue
                    full_url = _BASE + a_href if a_href.startswith("/") else a_href
                    fname = span.get_text(strip=True)
                    files.append({"url": full_url, "name": fname})
                if files:
                    result["files"] = files

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl kmcc.go.kr 보도자료 board (boardId=1113).

        Walks list pages until ``limit`` items are saved, the list is
        exhausted, duplicate URLs reappear, or the 25-minute wall-clock
        budget is hit.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_or_inf = limit if limit is not None else float("inf")

        try:
            for page_num in range(1, _MAX_PAGES + 1):
                elapsed = time.time() - start_time
                if elapsed > _CRAWL_BUDGET_SECS:
                    print(f"[{self.site_id}] Wall-clock budget exceeded ({elapsed:.0f}s). Stopping.")
                    break

                if saved >= limit_or_inf:
                    break

                if page_num % 10 == 0:
                    lbl = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lbl}")

                params = {**_LIST_PARAMS_BASE, "cp": str(page_num), "nop": str(_PAGE_SIZE)}
                list_url = _LIST_URL + "?" + urlencode(params)
                list_html = self._curl(list_url)
                if not list_html:
                    print(f"[{self.site_id}] Could not fetch list page {page_num}. Stopping.")
                    break

                items = self._parse_list(list_html)
                if not items:
                    print(f"[{self.site_id}] No items on page {page_num}. Done.")
                    break

                new_items = [it for it in items if it["detail_url"] not in seen_urls]
                if not new_items:
                    print(f"[{self.site_id}] All items on page {page_num} already seen. Stopping.")
                    break

                for item in new_items:
                    if saved >= limit_or_inf:
                        break

                    detail_url = item["detail_url"]
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    try:
                        time.sleep(self._delay if self._delay else _RATE_LIMIT)

                        detail_html = None
                        for attempt in range(3):
                            detail_html = self._curl(detail_url)
                            if detail_html:
                                break
                            wait = [1, 3, 9][attempt]
                            if attempt < 2:
                                print(
                                    f"[{self.site_id}] detail fetch failed "
                                    f"(attempt {attempt + 1}/3) {detail_url}, retry in {wait}s"
                                )
                                time.sleep(wait)

                        if not detail_html:
                            print(f"[{self.site_id}] Gave up fetching detail: {detail_url}")
                            continue

                        detail = self._parse_detail(detail_html)

                        title = detail.get("title") or item["title"]
                        title = re.sub(r"\s+", " ", title).strip()
                        if not title:
                            print(f"[{self.site_id}] No title for boardSeq={item['board_seq']}, skipping.")
                            continue

                        content = (detail.get("content") or "").strip()
                        parts = [title]
                        if content:
                            parts.append(content)
                        dept = detail.get("department") or item.get("department", "")
                        if dept:
                            parts.append(f"담당부서: {dept}")
                        abstract = "\n\n".join(parts)

                        if len(abstract) < _SKIP_ABSTRACT_UNDER:
                            print(
                                f"[{self.site_id}] Abstract too short "
                                f"({len(abstract)} chars) for '{title[:40]}', skipping."
                            )
                            continue

                        listed_date = item.get("listed_date", "")
                        published_date = detail.get("registered_date") or listed_date

                        all_files = detail.get("files") or item.get("file_links", [])
                        pdf_url = None
                        original_filename = None
                        for f in all_files:
                            fname = f.get("name", "")
                            if fname.lower().endswith(".pdf"):
                                pdf_url = f["url"]
                                original_filename = fname
                                break
                        if original_filename is None and all_files:
                            original_filename = all_files[0].get("name")
                            if pdf_url is None:
                                pdf_url = all_files[0].get("url")

                        author = detail.get("author", "")
                        license_type = detail.get("license_type") or item.get("license_type", "")
                        contact = detail.get("contact", "")

                        metadata = {
                            "posted_date": listed_date,
                            "originalFilename": original_filename,
                            "boardSeq": item["board_seq"],
                            "post_number_listed": item["post_number"],
                            "license_type": license_type,
                            "contact": contact,
                            "attachments": [
                                {"name": f.get("name", ""), "url": f.get("url", "")}
                                for f in all_files
                            ],
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": item["board_seq"],
                            "post_number": item["post_number"] or item["board_seq"],
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": author,
                            "publisher": "방송미디어통신위원회",
                            "department": dept,
                            "journal": None,
                            "url": detail_url,
                            "pdf_url": pdf_url,
                            "keywords": "",
                            "category": "보도자료",
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lbl = f"{saved}/{limit}" if limit is not None else str(saved)
                        print(f"[{self.site_id}] Saved {lbl}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item boardSeq={item.get('board_seq', '?')} failed: {exc}; continuing.")
                        continue

                if page_num >= _MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                    break

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
