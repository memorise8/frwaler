# -*- coding: utf-8 -*-
"""한국청소년정책연구원 (NYPI) 보도자료 board crawler."""

import html as html_mod
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Ensure project root on sys.path for absolute imports (no package context via spec_from_file_location)
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE_URL = "https://www.nypi.re.kr"
_LIST_PATH = "/board"
_MENU_KEY = "uCjzEQTnJu"
_BBS_ID = "BOARD00019"
_ROW_CNT = 10
_MAX_PAGES = 200
_MAX_SECONDS = 25 * 60  # 25 minutes wall-clock budget


def _make_soup(raw: str):
    """Build BeautifulSoup with parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _strip_tags(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


class NypiReBoardCrawler(BaseCrawler):
    """Crawler for NYPI 보도자료 board (BOARD00019)."""

    site_id = "nypi-re-kr-board"
    site_name = "Custom: nypi-re-kr-board"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """Fetch URL via curl with TLS workaround; returns decoded text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                time.sleep(wait)
        return None

    def _fetch_list_page(self, page: int) -> str | None:
        url = (
            f"{_BASE_URL}{_LIST_PATH}"
            f"?menuKey={_MENU_KEY}&bbsId={_BBS_ID}"
            f"&pageNum={page}&rowCnt={_ROW_CNT}"
        )
        return self._curl_get(url)

    def _fetch_detail_page(self, reg_no: str) -> str | None:
        url = (
            f"{_BASE_URL}/board/view"
            f"?menuKey={_MENU_KEY}&bbsId={_BBS_ID}"
            f"&pageNum=1&rowCnt={_ROW_CNT}&regNo={reg_no}"
        )
        return self._curl_get(url)

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list(self, raw: str) -> list[dict]:
        """Parse board list page HTML; return list of item dicts."""
        items = []
        try:
            soup = _make_soup(raw)
            if not soup:
                return items
            for row in soup.find_all("tr"):
                # regNo from the viewFiles anchor attribute (avoids &reg; entity issue in hrefs)
                files_link = row.find("a", class_="viewFiles")
                if not files_link:
                    continue
                reg_no = (files_link.get("regNo") or files_link.get("regno") or "").strip()
                if not reg_no:
                    continue

                # Title from the board/view anchor's title attribute
                title_link = row.find("a", href=re.compile(r"/board/view"))
                title = ""
                if title_link:
                    title = (title_link.get("title") or title_link.get_text(strip=True) or "").strip()
                title = html_mod.unescape(title)

                # Date
                date_td = row.find("td", attrs={"aria-label": "등록일"})
                date = date_td.get_text(strip=True) if date_td else ""

                # Author / department
                author_td = row.find("td", attrs={"aria-label": "작성자"})
                author = author_td.get_text(strip=True) if author_td else ""

                detail_url = (
                    f"{_BASE_URL}/board/view"
                    f"?menuKey={_MENU_KEY}&bbsId={_BBS_ID}"
                    f"&pageNum=1&rowCnt={_ROW_CNT}&regNo={reg_no}"
                )
                items.append({
                    "reg_no": reg_no,
                    "title": title,
                    "date": date,
                    "author": author,
                    "url": detail_url,
                })
        except Exception as exc:
            print(f"[{self.site_id}] list parse error: {exc}")
        return items

    def _parse_detail(self, raw: str) -> dict:
        """Parse detail page; return dict with 'abstract'."""
        result = {"abstract": ""}
        try:
            soup = _make_soup(raw)
            if not soup:
                return result
            content_div = soup.find("div", class_="board-content-view")
            if content_div:
                text = content_div.get_text(separator=" ", strip=True)
                result["abstract"] = re.sub(r"\s+", " ", text).strip()
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error: {exc}")
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        page = 1
        while True:
            # Limit check
            if limit is not None and saved >= limit:
                break

            # Safety caps
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break
            elapsed = time.time() - start_time
            if elapsed > _MAX_SECONDS:
                print(f"[{self.site_id}] Time budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Fetch list page with retry
            raw_list = None
            for attempt in range(3):
                raw_list = self._fetch_list_page(page)
                if raw_list:
                    break
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] List page {page} fetch failed (attempt {attempt+1}/3), retrying in {wait}s...")
                time.sleep(wait)

            if not raw_list:
                print(f"[{self.site_id}] Could not fetch list page {page}. Stopping.")
                break

            items = self._parse_list(raw_list)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # URL deduplication — detect silent pagination loop
            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break
            for it in new_items:
                seen_urls.add(it["url"])

            # Process each item
            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                try:
                    time.sleep(self._delay)

                    # Fetch detail with retry
                    raw_detail = None
                    for attempt in range(3):
                        raw_detail = self._fetch_detail_page(item["reg_no"])
                        if raw_detail:
                            break
                        wait = [1, 3, 9][attempt]
                        print(f"[{self.site_id}] Detail fetch failed for {item['reg_no']} "
                              f"(attempt {attempt+1}/3), retrying in {wait}s...")
                        time.sleep(wait)

                    if not raw_detail:
                        print(f"[{self.site_id}] Could not fetch detail for {item['reg_no']}. Skipping.")
                        continue

                    detail = self._parse_detail(raw_detail)
                    abstract = detail["abstract"]

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Abstract too short ({len(abstract)} chars) "
                              f"for {item['reg_no']}. Skipping.")
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": item["reg_no"],
                        "title": item["title"],
                        "authors": json.dumps(
                            [item["author"]] if item["author"] else [],
                            ensure_ascii=False,
                        ),
                        "abstract": abstract,
                        "category": "보도자료",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": item["date"],
                        "url": item["url"],
                        "pdf_url": "",
                        "doi": "",
                        "department": item["author"],
                        "metadata": json.dumps(
                            {"reg_no": item["reg_no"], "board_id": _BBS_ID},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('reg_no', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
