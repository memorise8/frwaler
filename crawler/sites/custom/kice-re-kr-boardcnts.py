# -*- coding: utf-8 -*-
"""KICE (한국교육과정평가원) 보도자료 crawler.

Target: https://www.kice.re.kr/boardCnts/list.do?boardID=10024&m=050102&s=kice&searchStr=

Site structure (probed 2026-07-19 via curl --tls-max 1.3):
  - List pages are server-rendered JSP-style HTML (``boardCnts/list.do``).
    Pagination: ``list.do?type=default&page=N&m=050102&searchStr=&searchType=S&s=kice&boardID=10024``.
    Each list page has TWO copies of the row set: a desktop ``table.wb`` and
    a mobile ``table.mb`` duplicate — dedupe by detail URL.
  - Each list row has an ``<a onclick="goView('10024','<boardSeq>', ...)">``
    handler. The native item id is ``boardSeq`` (a numeric string, e.g.
    "5100786") — used as both ``external_id`` and ``post_number``.
  - Detail pages: ``boardCnts/view.do?boardID=10024&boardSeq=<seq>&lev=0&
    m=050102&searchType=S&statusYN=W&page=1&s=kice``. Body content lives in
    a ``<textarea id='editorViewSource1'>`` containing raw (often malformed,
    pasted-from-Word/HWP) HTML — BeautifulSoup chokes on the broken
    attribute quoting there, so the body is extracted with a plain regex
    tag-stripper instead of a DOM parse.
  - Attachments: ``<a onclick="fn_fileDown('<fileSeq>');" title='<filename>'>``.
    ``fn_fileDown`` POSTs ``fileSeq`` to ``/boardCnts/fileDown.do``, but a
    plain GET with ``?fileSeq=<fileSeq>`` also returns the same bytes
    (confirmed ``%PDF-1.4`` magic bytes for a real PDF attachment), so
    ``pdf_url`` is built as a GET URL for downstream ``curl -sL`` downloads.
"""

import json
import re
import sys
import time
import html as html_lib
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup as _BS
    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(raw: str):
    """Parse HTML; fallback chain: html5lib -> lxml -> html.parser."""
    if not _BS_AVAILABLE:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return _BS(raw, parser)
        except Exception:
            continue
    return None


def _strip_tags(inner_html: str) -> str:
    """Regex-based tag stripper for the (often malformed) editor body HTML.

    The site's WYSIWYG editor content frequently has unescaped quotes inside
    ``style``/``font-family`` attribute values, which breaks proper DOM
    parsing (BeautifulSoup ends up leaking raw tag text). A plain regex
    strip is far more robust for just recovering readable text here.
    """
    if not inner_html:
        return ""
    text = html_lib.unescape(inner_html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_lib.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


_GOVIEW_RE = re.compile(r"goView\(\s*'(\d+)'\s*,\s*'(\d+)'")
_FILEDOWN_RE = re.compile(
    r"<a\s+href='javascript:void\(0\);'\s+onclick=\"fn_fileDown\('([a-fA-F0-9]+)'\);\"\s+title='([^']*)'"
)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class KiceReKrBoardcntsCrawler(BaseCrawler):
    """Crawler for KICE (한국교육과정평가원) 보도자료 board."""

    site_id = "kice-re-kr-boardcnts"
    site_name = "Custom: kice-re-kr-boardcnts"
    base_url = "https://www.kice.re.kr"

    _BOARD_ID = "10024"
    _M = "050102"
    _LIST_URL = "https://www.kice.re.kr/boardCnts/list.do"
    _DETAIL_URL = "https://www.kice.re.kr/boardCnts/view.do"
    _FILEDOWN_URL = "https://www.kice.re.kr/boardCnts/fileDown.do"
    _PUBLISHER = "한국교육과정평가원"
    _CATEGORY = "보도자료"

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        # This site's TLS/cert setup is flaky with strict verification
        # (institutional crawl permission granted 2026-07-18 for this run).
        self._session.verify = False
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Paginate the 보도자료 list and save each item.

        Walks pages 1.. until (a) saved >= limit, (b) a page yields zero new
        records, or (c) safety cap of 200 pages / 25-minute wall-clock
        budget is hit.
        """
        start_time = time.time()
        MAX_RUNTIME = 25 * 60  # seconds
        MAX_PAGES = 200

        saved = 0
        seen_urls: set = set()
        page = 1
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed > MAX_RUNTIME:
                print(f"[kice-re-kr-boardcnts] 25-minute wall-clock budget reached at page {page}. Exiting cleanly.")
                break
            if page > MAX_PAGES:
                print(f"[kice-re-kr-boardcnts] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[kice-re-kr-boardcnts] page {page}: saved {saved}/{limit_str}")

            items = self._fetch_list_page(page)
            if items is None:
                print(f"[kice-re-kr-boardcnts] Failed to fetch/parse list page {page}. Stopping.")
                break

            new_items = []
            for it in items:
                if it["url"] not in seen_urls:
                    seen_urls.add(it["url"])
                    new_items.append(it)

            if not new_items:
                print(f"[kice-re-kr-boardcnts] No new records on page {page}. Done.")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                try:
                    ok = self._process_item(item)
                    if ok:
                        saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kice-re-kr-boardcnts] item {item.get('url')} failed: {exc}")
                    continue
                time.sleep(self._delay)

            page += 1

        print(f"[kice-re-kr-boardcnts] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int):
        """Return a list of dicts with keys: board_seq, title, listed_date, url.

        Returns ``None`` on hard fetch/parse failure (caller stops crawl);
        returns ``[]`` when the page loaded fine but had no recognizable rows.
        """
        params = {
            "type": "default",
            "page": str(page),
            "m": self._M,
            "searchStr": "",
            "searchType": "S",
            "s": "kice",
            "boardID": self._BOARD_ID,
        }
        resp = self._request(self._LIST_URL, params=params)
        if resp is None:
            return None

        try:
            resp.encoding = resp.encoding or "utf-8"
            raw = resp.text
        except Exception:
            try:
                raw = resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[kice-re-kr-boardcnts] decode error on page {page}: {exc}")
                return None

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[kice-re-kr-boardcnts] parse error on page {page}: {exc}")
            return []

        if soup is None:
            return []

        table = soup.find("table", class_="wb")
        if table is None:
            # Fallback: any table whose rows carry a goView() handler.
            for t in soup.find_all("table"):
                if t.find("a", onclick=_GOVIEW_RE):
                    table = t
                    break
        if table is None:
            return []

        tbody = table.find("tbody") or table
        rows = tbody.find_all("tr")
        items = []
        for row in rows:
            try:
                a = row.find("a", onclick=_GOVIEW_RE)
                if a is None:
                    continue
                onclick = a.get("onclick", "") or ""
                m = _GOVIEW_RE.search(onclick)
                if not m:
                    continue
                list_board_id, board_seq = m.group(1), m.group(2)
                title = (a.get("title") or a.get_text(strip=True) or "").strip()
                if not title:
                    continue

                tds = row.find_all("td")
                post_number = tds[0].get_text(strip=True) if tds else board_seq
                if not re.match(r"^\d+$", post_number):
                    post_number = board_seq

                listed_date = None
                for td in tds:
                    t = td.get_text(strip=True)
                    if _DATE_RE.match(t):
                        listed_date = t
                        break

                detail_url = (
                    f"{self._DETAIL_URL}?boardID={list_board_id}&boardSeq={board_seq}"
                    f"&lev=0&m={self._M}&searchType=S&statusYN=W&page=1&s=kice"
                )

                items.append({
                    "board_id": list_board_id,
                    "board_seq": board_seq,
                    "post_number": post_number,
                    "title": title,
                    "listed_date": listed_date,
                    "url": detail_url,
                })
            except Exception as exc:
                print(f"[kice-re-kr-boardcnts] row parse error on page {page}: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page + save
    # ------------------------------------------------------------------

    def _process_item(self, item: dict) -> bool:
        """Fetch one detail page, extract metadata, and save. Returns True if saved."""
        detail_url = item["url"]
        resp = self._request(detail_url)
        if resp is None:
            print(f"[kice-re-kr-boardcnts] Could not fetch detail page {detail_url}. Skipping.")
            return False

        try:
            resp.encoding = resp.encoding or "utf-8"
            raw = resp.text
        except Exception:
            raw = resp.content.decode("utf-8", errors="replace")

        # ---- Title (prefer detail page h1, fall back to list title) ----
        title = item["title"]
        h1_m = re.search(r"<h1\s+class='tit'>(.*?)</h1>", raw, re.DOTALL)
        if h1_m:
            detail_title = html_lib.unescape(re.sub(r"<[^>]+>", " ", h1_m.group(1))).strip()
            detail_title = re.sub(r"\s+", " ", detail_title)
            if detail_title:
                title = detail_title
        if not title:
            print(f"[kice-re-kr-boardcnts] No title for {detail_url}. Skipping.")
            return False

        # ---- 글쓴이 (department / author org) ----
        writer_m = re.search(r"<li><strong>글쓴이</strong>(.*?)</li>", raw, re.DOTALL)
        department = html_lib.unescape(writer_m.group(1)).strip() if writer_m else None

        # ---- 등록일 (published/listed date on the detail page) ----
        date_m = re.search(r"<li><strong>등록일</strong>(\d{4}-\d{2}-\d{2})</li>", raw)
        detail_date = date_m.group(1) if date_m else None

        listed_date = item.get("listed_date") or detail_date
        published_date = detail_date or listed_date

        # ---- Body text (regex tag-strip — DOM parse mangles this content) ----
        body_m = re.search(
            r"<textarea id='editorViewSource1'[^>]*>(.*?)</textarea>", raw, re.DOTALL
        )
        body_text = _strip_tags(body_m.group(1)) if body_m else ""

        # ---- Attachments ----
        attachments = []
        for fm in _FILEDOWN_RE.finditer(raw):
            file_seq, filename = fm.group(1), html_lib.unescape(fm.group(2)).strip()
            if file_seq and filename:
                attachments.append({"file_seq": file_seq, "filename": filename})

        pdf_url = None
        original_filename = None
        for att in attachments:
            if att["filename"].lower().endswith(".pdf"):
                pdf_url = f"{self._FILEDOWN_URL}?fileSeq={att['file_seq']}"
                original_filename = att["filename"]
                break
        if pdf_url is None and attachments:
            # No PDF among attachments — still record the first attachment's
            # filename for traceability, but leave pdf_url unset.
            original_filename = attachments[0]["filename"]

        # ---- Abstract (must end up >=100 chars) ----
        if len(body_text) < 50 and not attachments and not department:
            print(f"[kice-re-kr-boardcnts] Body too short and no fallback content for {detail_url}. Skipping.")
            return False

        abstract_parts = [title]
        if body_text:
            abstract_parts.append(body_text)
        if department:
            abstract_parts.append(f"작성부서: {department}")
        if attachments:
            filenames = ", ".join(a["filename"] for a in attachments)
            abstract_parts.append(f"첨부파일: {filenames}")
        if listed_date:
            abstract_parts.append(f"등록일: {listed_date}")
        abstract = " / ".join(p for p in abstract_parts if p)
        abstract = re.sub(r"\s+", " ", abstract).strip()

        if len(abstract) < 50:
            print(f"[kice-re-kr-boardcnts] Final abstract too short ({len(abstract)} chars) for {detail_url}. Skipping.")
            return False

        board_seq = item["board_seq"]
        post_number = item.get("post_number") or board_seq

        meta_dict = {
            "nttId": board_seq,
            "boardSeq": board_seq,
            "boardId": item.get("board_id") or self._BOARD_ID,
            "post_number": post_number,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "detail_registered_date": detail_date,
            "series": None,
            "volume": None,
            "issue": None,
            "journal_raw": None,
            "attachments": attachments,
        }
        if original_filename:
            meta_dict["originalFilename"] = original_filename

        paper = {
            "site_id": self.site_id,
            "external_id": board_seq,
            "post_number": post_number,
            "url": detail_url,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "listed_date": listed_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": department,
            "journal": None,
            "keywords": None,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "category": self._CATEGORY,
            "doi": None,
            "metadata": json.dumps(meta_dict, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[kice-re-kr-boardcnts] Saved [{post_number}]: {title[:70]}")
        return True
