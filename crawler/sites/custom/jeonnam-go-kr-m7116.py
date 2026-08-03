# -*- coding: utf-8 -*-
"""전라남도청 보도자료 crawler — jeonnam.go.kr M7116."""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__)))))

from crawler.base_crawler import BaseCrawler

_SITE_ID = "jeonnam-go-kr-m7116"
_BASE = "https://www.jeonnam.go.kr"
_MENU_ID = "jeonnam0202000000"
_BOARD_ID = "M7116"
_LIST_URL = f"{_BASE}/{_BOARD_ID}/boardList.do"
_DETAIL_URL = f"{_BASE}/{_BOARD_ID}/boardView.do"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _curl(url: str) -> str | None:
    """GET via curl with TLS 1.3, 3-try exponential backoff."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30", "-L",
        "-H", ("User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
               "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        url,
    ]
    for attempt in range(3):
        try:
            res = subprocess.run(cmd, capture_output=True, timeout=35)
            if res.stdout:
                return res.stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            print(f"[{_SITE_ID}] curl timeout (attempt {attempt+1}/3): {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt+1}/3): {exc}")
        if attempt < 2:
            wait = 3 ** (attempt + 1)  # 3, 9
            print(f"[{_SITE_ID}] retrying in {wait}s...")
            time.sleep(wait)
    print(f"[{_SITE_ID}] curl failed after 3 attempts: {url}")
    return None


def _parse(html: str):
    """Parse HTML; fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class JeonnamBoardCrawler(BaseCrawler):
    """전라남도청 보도자료 (M7116)."""

    site_id = _SITE_ID
    site_name = "Custom: jeonnam-go-kr-m7116"
    base_url = _BASE

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        start_time = time.monotonic()

        while True:
            # Time budget
            if time.monotonic() - start_time > _TIMEOUT_SECS:
                print(f"[{_SITE_ID}] 25-min budget reached. Stopping.")
                break

            # Limit check
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{lim_str}")

            # Fetch list page
            list_url = f"{_LIST_URL}?menuId={_MENU_ID}&pageIndex={page}"
            raw = _curl(list_url)
            if not raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            soup = _parse(raw)
            if soup is None:
                print(f"[{_SITE_ID}] Failed to parse list page {page}. Stopping.")
                break

            tbody = soup.find("tbody")
            if not tbody:
                print(f"[{_SITE_ID}] No tbody on page {page}. Done.")
                break

            rows = tbody.find_all("tr")
            if not rows:
                print(f"[{_SITE_ID}] No rows on page {page}. Done.")
                break

            new_this_page = 0

            for row in rows:
                if limit is not None and saved >= limit:
                    break

                # Extract article link
                link = row.find("a", href=re.compile(r"/M7116/boardView\.do"))
                if not link:
                    continue

                href = link.get("href", "").replace("&amp;", "&")
                detail_url = href if href.startswith("http") else _BASE + href

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_this_page += 1

                # seq from URL
                m = re.search(r"seq=(\d+)", detail_url)
                seq = m.group(1) if m else None

                # listed_date and post_number from list row cells
                cells = row.find_all("td")
                board_post_num = cells[0].get_text(strip=True) if cells else None
                listed_date = None
                if len(cells) >= 4:
                    d = cells[3].get_text(strip=True)
                    if re.match(r"\d{4}-\d{2}-\d{2}", d):
                        listed_date = d

                # Fetch detail page — per-item isolation
                try:
                    self._fetch_and_save(detail_url, seq, board_post_num, listed_date)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{_SITE_ID}] Saved {saved}/{lim_str}: seq={seq}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item seq={seq} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if new_this_page == 0:
                print(f"[{_SITE_ID}] No new items on page {page}. Done.")
                break

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    def _fetch_and_save(self, detail_url: str, seq: str | None,
                        board_post_num: str | None, listed_date: str | None) -> None:
        raw = _curl(detail_url)
        if not raw:
            raise RuntimeError(f"fetch failed: {detail_url}")

        soup = _parse(raw)
        if soup is None:
            raise RuntimeError(f"parse failed: {detail_url}")

        bbs = soup.find(class_=lambda x: x and "bbs_view" in (
            " ".join(x) if isinstance(x, list) else x))
        if not bbs:
            raise RuntimeError("no bbs_view div")

        # Title
        h3 = bbs.find("h3", class_="view_title")
        title = h3.get_text(strip=True) if h3 else ""
        if not title:
            raise RuntimeError("no title")

        # Metadata: 작성일, 담당부서
        published_date = None
        department = None
        data_div = bbs.find(class_="data")
        if data_div:
            item_titles = data_div.find_all("span", class_="itemTitle")
            item_names = data_div.find_all("span", class_="name")
            for label_el, val_el in zip(item_titles, item_names):
                label = label_el.get_text(strip=True)
                val = val_el.get_text(strip=True)
                if "작성일" in label:
                    published_date = val
                elif "담당부서" in label:
                    department = val

        # Abstract from bbs_view_contnet
        contnet = soup.find(class_=lambda x: x and "bbs_view_contnet" in (
            " ".join(x) if isinstance(x, list) else x))
        abstract = ""
        if contnet:
            abstract = re.sub(r"\s+", " ", contnet.get_text(" ", strip=True)).strip()

        if len(abstract) < 50:
            print(f"[{_SITE_ID}] Skipping seq={seq}: abstract too short ({len(abstract)} chars)")
            raise RuntimeError(f"abstract too short ({len(abstract)} chars)")

        # Attachments: prefer PDF, fall back to HWP
        dl_div = bbs.find(class_="download")
        pdf_url = None
        original_filename = None
        all_files: list[dict] = []

        if dl_div:
            for li in dl_div.find_all("li"):
                fn_span = li.find("span", class_="fileNM")
                fn = fn_span.get_text(strip=True) if fn_span else ""
                a_tag = li.find("a", class_="mobileDownload")
                a_href = a_tag.get("href", "") if a_tag else ""
                if a_href:
                    full_href = a_href if a_href.startswith("http") else _BASE + a_href
                    all_files.append({"filename": fn, "url": full_href})
                    fn_lo = fn.lower()
                    if fn_lo.endswith(".pdf") and pdf_url is None:
                        pdf_url = full_href
                        original_filename = fn
                    elif fn_lo.endswith(".hwp") and pdf_url is None:
                        pdf_url = full_href
                        original_filename = fn

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": seq,
            "post_number": seq,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "authors": "",
            "publisher": "전라남도청",
            "department": department or "",
            "journal": "",
            "keywords": "",
            "category": "보도자료",
            "doi": "",
            "metadata": json.dumps({
                "seq": seq,
                "boardId": _BOARD_ID,
                "menuId": _MENU_ID,
                "posted_date": listed_date,
                "department": department,
                "board_post_number": board_post_num,
                "attachments": all_files,
            }, ensure_ascii=False),
        }

        self._save_paper(paper)
