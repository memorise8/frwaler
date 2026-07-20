# -*- coding: utf-8 -*-
"""KICJ 한국형사법무정책연구원 연구보고서 crawler.

Target: https://www.kicj.re.kr/board.es?mid=a10101000000&bid=0001

List endpoint  : GET  /board.es?mid=a10101000000&bid=0001&nPage={n}
Detail endpoint: GET  /board.es?mid=a10101000000&bid=0001&act=view&list_no={id}&nPage={p}
Abstract (AJAX): POST /boardGetData.es  bid=0001, list_no={id}, cate=book2
Keywords (AJAX): GET  /ajaxBoardTagList.es?bid=0001&list_no={id}
PDF download   : GET  /boardDownload.es?bid=0001&list_no={id}&seq=1
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.kicj.re.kr"
_BID = "0001"
_MID = "a10101000000"
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
# curl helpers (SSL workaround)
# ---------------------------------------------------------------------------

def _curl(method: str, url: str, params: dict | None = None,
          post_data: dict | None = None, referer: str = "") -> str | None:
    """GET or POST via curl with --tls-max 1.3 and 3 retries."""
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{qs}"

    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if method.upper() == "POST":
        cmd += ["-X", "POST",
                "-H", "Content-Type: application/x-www-form-urlencoded",
                "-H", "X-Requested-With: XMLHttpRequest"]
        for k, v in (post_data or {}).items():
            cmd += ["--data-urlencode", f"{k}={v}"]
    cmd.append(full_url)

    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            text = r.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception:
            pass
        if attempt < 2:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


def _fetch_get(url: str, params: dict | None = None, referer: str = "") -> str | None:
    return _curl("GET", url, params=params, referer=referer)


def _fetch_post(url: str, data: dict, referer: str = "") -> str | None:
    return _curl("POST", url, post_data=data, referer=referer)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KicjReKrBoardesCrawler(BaseCrawler):
    """Crawler for KICJ 한국형사법무정책연구원 연구보고서."""

    site_id = "kicj-re-kr-boardes"
    site_name = "Custom: kicj-re-kr-boardes"
    base_url = _BASE

    _LIST_URL = f"{_BASE}/board.es"
    _DETAIL_URL = f"{_BASE}/board.es"
    _ABSTRACT_URL = f"{_BASE}/boardGetData.es"
    _KEYWORDS_URL = f"{_BASE}/ajaxBoardTagList.es"

    _ITEMS_PER_PAGE = 10
    _MAX_PAGES = 200
    _TIMEOUT_SECS = 25 * 60  # 25 minutes

    # ------------------------------------------------------------------

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

            # --- fetch list page ---
            list_html = _fetch_get(
                self._LIST_URL,
                params={"mid": _MID, "bid": _BID, "nPage": str(page)},
                referer=self.base_url,
            )
            if not list_html:
                print(f"[{self.site_id}] List page {page} failed after retries. Stopping.")
                break

            # --- parse list ---
            try:
                soup = _bs4(list_html)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error list page {page}: {exc}. Skipping.")
                continue

            ul = soup.find("ul", class_="report_list")
            if not ul:
                print(f"[{self.site_id}] No report_list on page {page}. Stopping.")
                break

            items = ul.find_all("li", recursive=False)
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

                list_no, paper = result
                abstract = paper.get("abstract", "")

                if len(abstract) < 50:
                    print(f"[{self.site_id}] Skipping {list_no}: abstract too short ({len(abstract)} chars)")
                    continue

                self._save_paper(paper)
                saved += 1
                new_on_page += 1
                title = paper.get("title", "")[:60]
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title}")

                time.sleep(self._delay)

            if new_on_page == 0 and page > 1:
                print(f"[{self.site_id}] No new items on page {page} (all seen). Done.")
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {self._MAX_PAGES} pages.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------

    def _process_item(self, li, page_num: int, seen_urls: set) -> tuple | None:
        """Parse one list <li> and fetch its detail + abstract. Returns (list_no, paper) or None."""
        # --- extract list_no ---
        link = li.find("a", class_="label")
        if not link:
            return None
        href = link.get("href", "")
        m = re.search(r"list_no=(\d+)", href)
        if not m:
            return None
        list_no = m.group(1)

        detail_url = (
            f"{self._DETAIL_URL}?mid={_MID}&bid={_BID}"
            f"&act=view&list_no={list_no}&nPage={page_num}"
        )

        if detail_url in seen_urls:
            return None
        seen_urls.add(detail_url)

        title_from_list = link.get_text(strip=True)

        # Basic metadata from list item
        info: dict[str, str] = {}
        for li_el in li.select("ul.info li"):
            k_el = li_el.find("span")
            v_el = li_el.find("strong")
            if k_el and v_el:
                info[k_el.get_text(strip=True)] = v_el.get_text(strip=True)

        # PDF download link from .view4
        pdf_url = ""
        dl = li.find("a", class_="view4")
        if dl:
            dh = dl.get("href", "")
            if dh:
                pdf_url = (_BASE + dh) if dh.startswith("/") else dh

        # --- fetch detail page ---
        detail_html = _fetch_get(
            detail_url,
            referer=f"{self._LIST_URL}?mid={_MID}&bid={_BID}&nPage={page_num}",
        )
        if not detail_html:
            print(f"[{self.site_id}] Detail fetch failed for list_no={list_no}")
            return None

        try:
            dsoup = _bs4(detail_html)
        except Exception as exc:
            print(f"[{self.site_id}] Detail parse error {list_no}: {exc}")
            return None

        # Title (from detail page h2.view_tit if available)
        title = title_from_list
        h2 = dsoup.find("h2", class_="view_tit")
        if h2:
            t = h2.get_text(strip=True)
            if t:
                title = t

        # --- metadata table ---
        tbl_info: dict[str, str] = {}
        tbl = dsoup.find("table")
        if tbl:
            for row in tbl.find_all("tr"):
                cells = row.find_all(["th", "td"])
                txts = [c.get_text(" ", strip=True) for c in cells]
                for i in range(0, len(txts) - 1, 2):
                    k, v = txts[i], txts[i + 1]
                    if k and v:
                        tbl_info[k] = v

        # Authors
        resp = tbl_info.get("연구책임자", "").strip()
        inner = tbl_info.get("내부연구참여자", "").strip()
        outer = tbl_info.get("외부참여연구자", "").strip()
        authors_list = [a for a in [resp, inner, outer] if a]
        if not authors_list and info.get("저자"):
            authors_list = [info["저자"]]

        # Published date
        pub_raw = tbl_info.get("출판일", info.get("출판일", "")).strip()
        published_date = ""
        if pub_raw:
            published_date = pub_raw.replace(".", "-")
            # Validate: keep only if YYYY-MM-DD
            try:
                datetime.strptime(published_date, "%Y-%m-%d")
            except ValueError:
                published_date = pub_raw

        # Other metadata fields
        isbn = tbl_info.get("ISBN", "")
        doc_type = tbl_info.get("자료유형", "")
        research_type = tbl_info.get("연구유형", "")
        classification = tbl_info.get("분류기호", "")
        pages = tbl_info.get("페이지", "")
        dept = tbl_info.get("발행기관", "한국형사·법무정책연구원")
        lang = tbl_info.get("언어", "")
        std_class = tbl_info.get("표준분류", "")
        registered_date = tbl_info.get("등록일", info.get("등록일", ""))

        # --- abstract via AJAX book2 endpoint ---
        abstract = self._fetch_abstract(list_no, detail_url)

        # Fallback: view_cont from detail page (contains TOC text — long enough)
        if len(abstract) < 50:
            vc = dsoup.find(class_="view_cont")
            if vc:
                abstract = re.sub(r"\s+", " ", vc.get_text(" ", strip=True)).strip()

        # --- keywords via AJAX ---
        keywords_list = self._fetch_keywords(list_no, detail_url)

        paper = {
            "site_id": self.site_id,
            "external_id": list_no,
            "title": title,
            "authors": json.dumps(authors_list, ensure_ascii=False),
            "abstract": abstract,
            "category": doc_type or "연구보고서",
            "keywords": json.dumps(keywords_list, ensure_ascii=False),
            "published_date": published_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": dept,
            "metadata": json.dumps({
                "list_no": list_no,
                "isbn": isbn,
                "pages": pages,
                "language": lang,
                "research_type": research_type,
                "classification": classification,
                "standard_class": std_class,
                "inner_authors": inner,
                "outer_authors": outer,
                "registered_date": registered_date,
            }, ensure_ascii=False),
        }
        return list_no, paper

    # ------------------------------------------------------------------

    def _fetch_abstract(self, list_no: str, referer: str) -> str:
        """Fetch abstract text via AJAX book2 endpoint."""
        html = _fetch_post(
            self._ABSTRACT_URL,
            data={"bid": _BID, "list_no": list_no, "cate": "book2"},
            referer=referer,
        )
        if not html or not html.strip():
            return ""
        try:
            soup = _bs4(html)
        except Exception:
            return _strip_tags(html)

        # Prefer div.IR (machine-readable text, not images)
        ir = soup.find("div", class_="IR")
        if ir:
            text = re.sub(r"\s+", " ", ir.get_text(" ", strip=True)).strip()
            if len(text) >= 50:
                return text

        # Fallback: strip all tags from the full response
        text = _strip_tags(html)
        return text

    def _fetch_keywords(self, list_no: str, referer: str) -> list:
        """Fetch keyword tags via AJAX."""
        html = _fetch_get(
            self._KEYWORDS_URL,
            params={"bid": _BID, "list_no": list_no},
            referer=referer,
        )
        if not html or not html.strip():
            return []
        try:
            soup = _bs4(html)
            return [a.get_text(strip=True) for a in soup.find_all("a") if a.get_text(strip=True)]
        except Exception:
            return []
