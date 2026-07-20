# -*- coding: utf-8 -*-
"""보건복지부 보도자료 crawler — mohw-go-kr-boardes.

List:   https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027&nPage=N
Detail: https://www.mohw.go.kr/board.es?mid=a10503010100&bid=0027&act=view&list_no=LIST_NO&tag=&nPage=N
Files:  /boardDownload.es?bid=0027&list_no=LIST_NO&seq=SEQ
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SITE_ID = "mohw-go-kr-boardes"
_BASE = "https://www.mohw.go.kr"
_MID = "a10503010100"
_BID = "0027"
_ABSTRACT_MIN_CHARS = 100


def _make_soup(html: str):
    """Parse HTML with best available parser; returns None rather than raising."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(html: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class MohwGovKrBoardesCrawler(BaseCrawler):
    site_id = "mohw-go-kr-boardes"
    site_name = "Custom: mohw-go-kr-boardes"
    base_url = "https://www.mohw.go.kr"

    # ------------------------------------------------------------------
    # Network helper
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, timeout: int = 30) -> str | None:
        """GET via curl (TLS max 1.3, Korean gov SSL quirks). Returns text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            url,
        ]
        waits = [1, 3, 9]
        for attempt, wait in enumerate(waits, start=1):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10
                )
                raw = result.stdout
                if raw:
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < len(waits):
                    print(f"[{_SITE_ID}] Empty response (attempt {attempt}/3), retrying in {wait}s...")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < len(waits):
                    print(f"[{_SITE_ID}] curl error (attempt {attempt}/3): {exc}, retrying in {wait}s...")
                    time.sleep(wait)
                else:
                    print(f"[{_SITE_ID}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parser
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Return list of item dicts from the board listing page. Empty list on failure."""
        url = f"{_BASE}/board.es?mid={_MID}&bid={_BID}&nPage={page}"
        html = self._curl(url)
        if not html:
            return []

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] List page parse error at page {page}: {exc}")
            return []
        if not soup:
            return []

        items = []
        try:
            table = soup.find("table", class_="tstyle_list")
            if not table:
                return []
            tbody = table.find("tbody")
            if not tbody:
                return []

            for row in tbody.find_all("tr"):
                try:
                    cells = row.find_all("td")
                    if len(cells) < 4:
                        continue

                    # 번호 — sequential post number (used for incremental MAX() check)
                    post_number = cells[0].get_text(strip=True)

                    # Title + list_no from anchor
                    a_tag = cells[1].find("a", class_="txt_title")
                    if not a_tag:
                        continue
                    # Remove decorative sub-elements before extracting text
                    for unwanted in a_tag.find_all(["span", "i"]):
                        unwanted.decompose()
                    title = a_tag.get_text(strip=True)
                    href = a_tag.get("href", "")
                    m = re.search(r"list_no=(\d+)", href)
                    if not m:
                        continue
                    list_no = m.group(1)

                    dept = cells[2].get_text(strip=True) if len(cells) > 2 else ""
                    listed_date = cells[3].get_text(strip=True) if len(cells) > 3 else ""

                    items.append({
                        "post_number": post_number,
                        "list_no": list_no,
                        "title": title,
                        "department": dept,
                        "listed_date": listed_date,
                    })
                except Exception as row_exc:
                    print(f"[{_SITE_ID}] Row parse error: {row_exc}")
                    continue
        except Exception as exc:
            print(f"[{_SITE_ID}] Table parse error at page {page}: {exc}")

        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, list_no: str, npage: int = 1) -> dict:
        """Fetch and parse a detail page. Returns dict (may be empty on failure)."""
        url = f"{_BASE}/board.es?mid={_MID}&bid={_BID}&act=view&list_no={list_no}&tag=&nPage={npage}"
        html = self._curl(url)
        if not html:
            return {}

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] Detail soup error for list_no={list_no}: {exc}")
            return {}
        if not soup:
            return {}

        result: dict = {}
        try:
            # The site uses <article class="board_view"> — not a div
            board_view = soup.find("article", class_="board_view")
            if not board_view:
                return {}

            # Metadata: 작성일 / 담당자 / 담당부서
            info_ul = board_view.find("ul", class_="info")
            if info_ul:
                for li in info_ul.find_all("li"):
                    try:
                        strong = li.find("strong")
                        if not strong:
                            continue
                        key = strong.get_text(strip=True)
                        val = ""
                        for sp in li.find_all("span"):
                            # Skip sr_only and display:none spans
                            cls = sp.get("class") or []
                            style = sp.get("style") or ""
                            if "sr_only" in cls or "display:none" in style:
                                continue
                            val = sp.get_text(strip=True)
                        if key == "작성일":
                            result["published_date"] = val[:10] if val else ""
                        elif key == "담당자":
                            result["contact_person"] = val
                        elif key == "담당부서":
                            result["department"] = val
                    except Exception:
                        continue

            # Body text
            contents_div = board_view.find("div", class_="contents")
            result["abstract"] = _strip_tags(str(contents_div)) if contents_div else ""

            # Attachments — collect all, prefer PDF for pdf_url
            file_div = board_view.find("div", class_="file")
            pdf_url: str | None = None
            original_filename: str | None = None
            all_files: list[dict] = []

            if file_div:
                for li in file_div.find_all("li"):
                    try:
                        for a in li.find_all("a"):
                            href = a.get("href", "")
                            if "boardDownload" not in href:
                                continue
                            fname = a.get("title", "")
                            full_url = (_BASE + href) if href.startswith("/") else href
                            all_files.append({"url": full_url, "title": fname})
                            if pdf_url is None and ".pdf" in fname.lower():
                                pdf_url = full_url
                                original_filename = fname
                    except Exception:
                        continue

            result["pdf_url"] = pdf_url
            result["original_filename"] = original_filename
            result["attachments"] = all_files

        except Exception as exc:
            print(f"[{_SITE_ID}] Detail parse error for list_no={list_no}: {exc}")

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl MOHW 보도자료 board.

        Paginates through nPage=1, 2, 3, … fetching detail for each item.
        Stops when (a) limit reached, (b) empty page, (c) all items seen
        (loop guard), or (d) safety cap / wall-clock budget hit.
        """
        saved = 0
        seen: set[str] = set()           # dedup by list_no
        start_time = time.time()
        max_seconds = 25 * 60            # 25-minute wall-clock budget
        safety_cap = 200
        limit_or_inf = limit if limit is not None else "∞"

        for page in range(1, safety_cap + 1):
            # --- budget guards ---
            if time.time() - start_time > max_seconds:
                print(f"[{_SITE_ID}] Wall-clock budget reached at page {page}, exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page == safety_cap:
                print(f"[{_SITE_ID}] Safety cap of {safety_cap} pages reached.")
                break

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_or_inf}")

            # --- fetch list ---
            items = self._fetch_list_page(page)
            if not items:
                print(f"[{_SITE_ID}] page {page}: empty list, stopping.")
                break

            # --- deduplicate ---
            new_items = [it for it in items if it["list_no"] not in seen]
            for it in new_items:
                seen.add(it["list_no"])

            if not new_items:
                print(f"[{_SITE_ID}] page {page}: all items already seen (paginator loop), stopping.")
                break

            # --- process each item ---
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                list_no = item["list_no"]
                detail_url = (
                    f"{_BASE}/board.es?mid={_MID}&bid={_BID}"
                    f"&act=view&list_no={list_no}&tag=&nPage={page}"
                )

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(list_no, npage=page)

                    title = item["title"]
                    if not title:
                        print(f"[{_SITE_ID}] item {list_no}: no title, skipping.")
                        continue

                    abstract = detail.get("abstract", "")
                    if len(abstract) < _ABSTRACT_MIN_CHARS:
                        print(
                            f"[{_SITE_ID}] item {list_no} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    listed_date = item["listed_date"]
                    published_date = detail.get("published_date", "") or listed_date
                    department = detail.get("department", "") or item.get("department", "")
                    contact = detail.get("contact_person", "")
                    pdf_url = detail.get("pdf_url") or None
                    original_filename = detail.get("original_filename")
                    attachments = detail.get("attachments", [])

                    paper = {
                        "site_id": self.site_id,
                        "external_id": list_no,
                        "post_number": item["post_number"],
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": original_filename,
                        "publisher": "보건복지부",
                        "department": department,
                        "authors": contact or None,
                        "keywords": None,
                        "category": "보도자료",
                        "doi": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": listed_date,
                                "originalFilename": original_filename,
                                "contact_person": contact,
                                "department": department,
                                "list_no": list_no,
                                "attachments": attachments,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {list_no} failed: {exc}")
                    continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
