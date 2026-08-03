# -*- coding: utf-8 -*-
"""방위사업청 보도자료 crawler.

Target: https://www.dapa.go.kr/dapa/doc/selectDocList.do?menuSeq=3069&bbsSeq=326

List page: GET selectDocList.do?menuSeq=3069&bbsSeq=326&currentPageNo={N}
  table.list-table tbody tr
    td.num              → post_number (sequential, e.g. 684)
    a.subject-anchor    → onclick fn_selectDoc('{docSeq}') → external_id
    td[2]               → listed_date (YYYY-MM-DD)

Detail page: GET selectDoc.do?docSeq={docSeq}&menuSeq=3069&bbsSeq=326&currentPageNo={N}
  div.view-subject h4.text          → title
  ul.view-info span.tit="게시일"     → listed_date (double-check)
  div.view-cont                     → abstract (full press-release text)
  ul.view-file li.file-item         → PDF/HWP download links
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.dapa.go.kr"
_MENU_SEQ = "3069"
_BBS_SEQ = "326"
_LIST_URL = f"{_BASE}/dapa/doc/selectDocList.do"
_DETAIL_URL = f"{_BASE}/dapa/doc/selectDoc.do"
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


def _clean_text(html_or_text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_or_text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# curl helper (TLS workaround for Korean gov sites)
# ---------------------------------------------------------------------------

def _curl_get(url: str, params: dict | None = None, referer: str = "") -> str | None:
    """GET via curl --tls-max 1.3 with 3-attempt exponential backoff."""
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{qs}"

    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", "Connection: keep-alive",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
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
            time.sleep([1, 3, 9][attempt])
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class DapaGoKrDapaCrawler(BaseCrawler):
    """Crawler for 방위사업청 보도자료."""

    site_id = "dapa-go-kr-dapa"
    site_name = "Custom: dapa-go-kr-dapa"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
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

            # Fetch list page
            list_html = _curl_get(
                _LIST_URL,
                params={"menuSeq": _MENU_SEQ, "bbsSeq": _BBS_SEQ, "currentPageNo": str(page)},
                referer=_BASE + "/",
            )
            if not list_html:
                print(f"[{self.site_id}] List page {page} fetch failed after retries. Stopping.")
                break

            try:
                soup = _bs4(list_html)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error list page {page}: {exc}. Skipping.")
                continue

            rows = soup.select("table.list-table tbody tr")
            if not rows:
                print(f"[{self.site_id}] No rows on page {page}. Done.")
                break

            new_on_page = 0

            for row in rows:
                if limit is not None and saved >= limit:
                    break

                try:
                    result = self._process_row(row, page, seen_urls)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] row error page {page}: {exc}; continuing")
                    continue

                if result is None:
                    continue

                doc_seq, paper = result
                abstract = paper.get("abstract", "")

                if len(abstract) < 50:
                    print(f"[{self.site_id}] Skipping docSeq={doc_seq}: abstract too short ({len(abstract)} chars)")
                    continue

                self._save_paper(paper)
                saved += 1
                new_on_page += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper.get('title', '')[:60]}")

                time.sleep(self._delay)

            if new_on_page == 0 and page > 1:
                print(f"[{self.site_id}] No new items on page {page} (all seen/skipped). Done.")
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {self._MAX_PAGES} pages.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    def _process_row(self, row, page_num: int, seen_urls: set):
        """Parse one list <tr> and fetch its detail page.

        Returns (doc_seq, paper_dict) or None.
        """
        # Post number from td.num
        num_td = row.find("td", class_="num")
        if not num_td:
            return None
        post_number = num_td.get_text(strip=True)

        # docSeq from onclick="fn_selectDoc('58672')"
        subj_a = row.find("a", class_="subject-anchor")
        if not subj_a:
            return None
        onclick = subj_a.get("onclick", "")
        m = re.search(r"fn_selectDoc\('(\d+)'\)", onclick)
        if not m:
            return None
        doc_seq = m.group(1)

        # listed_date from 3rd td (index 2)
        tds = row.find_all("td")
        listed_date = tds[2].get_text(strip=True) if len(tds) > 2 else ""

        detail_url = (
            f"{_DETAIL_URL}?docSeq={doc_seq}"
            f"&menuSeq={_MENU_SEQ}&bbsSeq={_BBS_SEQ}&currentPageNo={page_num}"
        )

        if detail_url in seen_urls:
            return None
        seen_urls.add(detail_url)

        # Fetch detail page
        list_referer = f"{_LIST_URL}?menuSeq={_MENU_SEQ}&bbsSeq={_BBS_SEQ}&currentPageNo={page_num}"
        detail_html = _curl_get(detail_url, referer=list_referer)
        if not detail_html:
            print(f"[{self.site_id}] Detail fetch failed for docSeq={doc_seq}")
            return None

        try:
            dsoup = _bs4(detail_html)
        except Exception as exc:
            print(f"[{self.site_id}] Detail parse error docSeq={doc_seq}: {exc}")
            return None

        # Title
        title_el = dsoup.select_one("div.view-subject h4.text")
        title = title_el.get_text(strip=True) if title_el else subj_a.get_text(strip=True)
        if not title:
            title = subj_a.get_text(strip=True)

        # Listed date confirmed from detail (게시일 span)
        pub_date = listed_date
        for info_li in dsoup.select("ul.view-info li.info-item"):
            tit_span = info_li.find("span", class_="tit")
            if tit_span and "게시일" in tit_span.get_text():
                p_el = info_li.find("p", class_="text")
                if p_el:
                    pub_date = p_el.get_text(strip=True)
                    break

        # Abstract from view-cont (full press release body)
        abstract = ""
        view_cont = dsoup.find("div", class_="view-cont")
        if view_cont:
            abstract = re.sub(r"\s+", " ", view_cont.get_text(" ", strip=True)).strip()

        # Fallback: if view-cont not found or too short, try board-view
        if len(abstract) < 50:
            bv = dsoup.find("div", id="board-view")
            if bv:
                abstract = re.sub(r"\s+", " ", bv.get_text(" ", strip=True)).strip()

        # Files: prefer PDF; fall back to first downloadable attachment
        pdf_url = None
        original_filename = None
        view_file = dsoup.find("ul", class_="view-file")
        if view_file:
            pdf_candidate = None
            first_candidate = None
            for file_li in view_file.find_all("li", class_="file-item"):
                for a in file_li.find_all("a", href=True):
                    href = a.get("href", "")
                    if "readDownloadFile" not in href:
                        continue
                    fname = a.get_text(strip=True)
                    # Skip button labels
                    if fname in ("다운로드", "미리보기") or not fname:
                        continue
                    full_href = (_BASE + href) if href.startswith("/") else href
                    if first_candidate is None:
                        first_candidate = (full_href, fname)
                    if fname.lower().endswith(".pdf") and pdf_candidate is None:
                        pdf_candidate = (full_href, fname)
            chosen = pdf_candidate or first_candidate
            if chosen:
                pdf_url, original_filename = chosen

        paper = {
            "site_id": self.site_id,
            "external_id": doc_seq,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "authors": None,
            "publisher": "방위사업청",
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "category": "보도자료",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps({
                "docSeq": doc_seq,
                "post_number": post_number,
                "bbsSeq": _BBS_SEQ,
                "menuSeq": _MENU_SEQ,
                "posted_date": listed_date,
                "originalFilename": original_filename,
            }, ensure_ascii=False),
        }
        return doc_seq, paper
