# -*- coding: utf-8 -*-
"""RDA 농촌진흥청 보도자료 crawler.

Target: https://www.rda.go.kr/board/board.do?mode=list&prgId=day_farmprmninfoEntry

List endpoint  : GET /board/board.do?prgId=day_farmprmninfoEntry&boardId=farmprmninfo
                     &mode=list&currPage={n}
Detail endpoint: GET /board/board.do?boardId=farmprmninfo&prgId=day_farmprmninfoEntry
                     &currPage={n}&dataNo={id}&mode=updateCnt
PDF download   : GET /fileDownLoadDw.do?boardId=farmprmninfo&dataNo={id}&sortNo={sort}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.rda.go.kr"
_BOARD_ID = "farmprmninfo"
_PRG_ID = "day_farmprmninfoEntry"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _bs4(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("All BeautifulSoup parsers failed")


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

def _curl_get(url: str) -> str | None:
    """GET via curl with --tls-max 1.3 and 3 retries (1s, 3s, 9s backoff)."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30", "-L",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        url,
    ]
    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            text = r.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception:
            pass
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class RdaGoKrBoardCrawler(BaseCrawler):
    """Crawler for RDA 농촌진흥청 보도자료."""

    site_id = "rda-go-kr-board"
    site_name = "Custom: rda-go-kr-board"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget
            if time.time() - start_time > self._TIMEOUT_SECS:
                elapsed = int(time.time() - start_time)
                print(f"[{self.site_id}] Time budget exceeded ({elapsed}s). Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = (
                f"{_BASE}/board/board.do?prgId={_PRG_ID}&boardId={_BOARD_ID}"
                f"&searchKey=&searchVal=&searchSDate=&searchEDate="
                f"&searchOrgDeptKey=&searchOrgDeptVal=&catgId="
                f"&mode=list&currPage={page}"
            )

            html = _curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] List page {page} fetch failed. Stopping.")
                break

            try:
                soup = _bs4(html)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error list page {page}: {exc}. Skipping.")
                continue

            ul = soup.find("ul", class_="krds-structured-list")
            if not ul:
                print(f"[{self.site_id}] No list found on page {page}. Stopping.")
                break

            items = ul.find_all("li", class_="structured-item")
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_on_page = 0

            for li in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    result = self._process_item(li, page, seen_urls)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item error page {page}: {exc}; continuing")
                    continue

                if result is None:
                    continue

                data_no, paper = result
                abstract = paper.get("abstract", "")

                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] Skipping {data_no}: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                self._save_paper(paper)
                saved += 1
                new_on_page += 1
                title = paper.get("title", "")[:60]
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title}")

                time.sleep(self._delay)

            if new_on_page == 0 and page > 1:
                print(f"[{self.site_id}] No new items on page {page}. Done.")
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {self._MAX_PAGES} pages.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------

    def _process_item(self, li, page_num: int, seen_urls: set) -> tuple | None:
        """Parse one list <li> and fetch its detail page. Returns (data_no, paper) or None."""
        link = li.find("a")
        if not link:
            return None

        href = link.get("href", "")
        m = re.search(r"dataNo=(\d+)", href)
        if not m:
            return None
        data_no = m.group(1)

        # Canonical detail URL
        detail_url = (
            f"{_BASE}/board/board.do?boardId={_BOARD_ID}"
            f"&prgId={_PRG_ID}&currPage={page_num}"
            f"&dataNo={data_no}&mode=updateCnt"
            f"&searchSDate=&searchEDate="
            f"&searchOrgDeptKey=allOrgDept&searchOrgDeptVal="
            f"&searchKey=&searchVal="
        )

        if detail_url in seen_urls:
            return None
        seen_urls.add(detail_url)

        # --- List-level fields ---
        tit_el = li.find("p", class_="c-tit")
        title_from_list = tit_el.get_text(strip=True) if tit_el else ""

        txt_el = li.find("p", class_="c-txt")
        list_abstract = txt_el.get_text(strip=True) if txt_el else ""

        date_el = li.find("p", class_="c-date")
        listed_date = date_el.get_text(strip=True) if date_el else ""

        # --- Fetch detail page ---
        detail_html = _curl_get(detail_url)
        if not detail_html:
            print(f"[{self.site_id}] Detail fetch failed for dataNo={data_no}")
            return None

        try:
            dsoup = _bs4(detail_html)
        except Exception as exc:
            print(f"[{self.site_id}] Detail parse error dataNo={data_no}: {exc}")
            return None

        # Title
        title = title_from_list
        h2 = dsoup.find("h2", class_="tit")
        if h2:
            t = h2.get_text(strip=True)
            if t:
                title = t

        # Author / date / department from .community-page-title .info
        author = ""
        published_date = ""
        department = ""
        page_title_div = dsoup.find("div", class_="community-page-title")
        if page_title_div:
            info_ul = page_title_div.find("ul", class_="info")
            if info_ul:
                for p_el in info_ul.find_all("p"):
                    strong = p_el.find("strong")
                    if not strong:
                        continue
                    label = strong.get_text(strip=True)
                    full_text = p_el.get_text(strip=True)
                    val = full_text.replace(label, "", 1).strip()
                    if label == "작성자":
                        author = val
                    elif label == "작성일":
                        published_date = val
                    elif label == "출처":
                        department = val

        # --- Abstract: full article body ---
        abstract = ""
        content_div = dsoup.find("div", id="contents")
        if content_div:
            raw_text = content_div.get_text(" ", strip=True)
            abstract = re.sub(r"\s+", " ", raw_text).strip()

        # Fallback: og:description meta tag
        if len(abstract) < 50:
            og = dsoup.find("meta", property="og:description")
            if og:
                og_text = og.get("content", "")
                og_text = re.sub(r"&[a-zA-Z0-9#]+;", " ", og_text)
                og_text = re.sub(r"\s+", " ", og_text).strip()
                if len(og_text) > len(abstract):
                    abstract = og_text

        # Fallback: list-level truncated text
        if len(abstract) < 50 and len(list_abstract) >= 50:
            abstract = list_abstract

        # --- File attachments ---
        pdf_url = None
        original_filename = None
        file_list_ul = dsoup.find("ul", id="file-list")
        if file_list_ul:
            files = self._parse_file_list(file_list_ul, data_no)
            # Prefer PDF, then fall back to first attachment
            for fname, furl in files:
                if fname.lower().endswith(".pdf"):
                    pdf_url = furl
                    original_filename = fname
                    break
            if not pdf_url and files:
                original_filename, pdf_url = files[0]

        paper = {
            "site_id": self.site_id,
            "external_id": data_no,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": author,
            "publisher": "농촌진흥청",
            "department": department,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": "",
            "category": "보도자료",
            "metadata": json.dumps(
                {
                    "dataNo": data_no,
                    "boardId": _BOARD_ID,
                    "prgId": _PRG_ID,
                    "posted_date": listed_date,
                    "department": department,
                },
                ensure_ascii=False,
            ),
        }
        return data_no, paper

    def _parse_file_list(self, ul, data_no: str) -> list[tuple[str, str]]:
        """Return list of (clean_filename, download_url) from <ul id='file-list'>."""
        results = []
        for li in ul.find_all("li"):
            name_el = li.find("p", class_="name")
            if not name_el:
                continue
            raw_name = name_el.get_text(strip=True)
            # Remove size suffix like " [382.00 KB]"
            clean_name = re.sub(r"\s*\[\d[\d., KMGBkb]+\]\s*$", "", raw_name).strip()
            if not clean_name:
                continue

            # Find fn_download onclick
            btn = li.find("button", onclick=re.compile(r"fn_download"))
            if not btn:
                continue
            onclick = btn.get("onclick", "")
            sm = re.search(
                r"fn_download\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]?(\d+)['\"]?\s*\)",
                onclick,
            )
            if not sm:
                continue
            sort_no = sm.group(3)
            dl_url = (
                f"{_BASE}/fileDownLoadDw.do"
                f"?boardId={_BOARD_ID}&dataNo={data_no}&sortNo={sort_no}"
            )
            results.append((clean_name, dl_url))
        return results
