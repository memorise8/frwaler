# -*- coding: utf-8 -*-
"""세종특별자치시 보도자료 (R0079) BBS crawler.

Starting URL: https://www.sejong.go.kr/bbs/R0079/list.do

List page structure (eGovFrame board, plain HTML table):
  - <tr> rows in <table>/<tbody>; columns keyed by data-cell-header:
    번호 (post number), 제목/subject (title + <a href="view.do?nttId=...">),
    작성자 (department), 조회수, 등록일 (YYYY-MM-DD), 첨부파일.
  - Pagination: list.do?pageIndex=N (plain GET, no session/token needed).

Detail page structure:
  - Title: h2.ui.bbs--view--tit
  - Meta: span><i>작성자</i>DEPT, span><i>등록일</i>YYYY-MM-DD (in .bbs--view--header)
  - Content: div.ui.bbs--view--content (article <p> tags)
  - Attachments: <a href="javascript:fn_egov_downFile('ATCH_FILE_ID','FILE_SN')">FILENAME [SIZE]</a>
    Download URL: /cmm/fms/FileDown.do?atchFileId=X&fileSn=Y
"""

import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_SITE_ID = "sejong-go-kr-bbs"
_BASE_URL = "https://www.sejong.go.kr"
_LIST_URL = f"{_BASE_URL}/bbs/R0079/list.do"
_VIEW_URL = f"{_BASE_URL}/bbs/R0079/view.do"
_FILE_URL = f"{_BASE_URL}/cmm/fms/FileDown.do"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60  # 25 minutes wall-clock cap
_MIN_ABSTRACT_LEN = 50         # skip items below this
_SAVE_ABSTRACT_LEN = 100       # pad with metadata if abstract is still short


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, referer: str = _LIST_URL, timeout: int = 30):
    """GET via curl with up to 3 retries (1s, 3s, 9s backoff). Returns bytes or None."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "-L",
        "--max-time", str(timeout),
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", f"Referer: {referer}",
        url,
    ]
    backoffs = (1, 3, 9)
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout and len(result.stdout) > 200:
                return result.stdout
            print(f"[{_SITE_ID}] curl small/empty response (attempt {attempt + 1}/3) for {url}")
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/3) for {url}: {exc}")
        if attempt < 2:
            time.sleep(backoffs[attempt])
    print(f"[{_SITE_ID}] curl failed after 3 attempts: {url}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(raw):
    """BeautifulSoup with html5lib -> lxml -> html.parser fallback chain."""
    from bs4 import BeautifulSoup
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    last_exc = None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(text, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No suitable HTML parser available: {last_exc}")


def _parse_date(raw: str) -> str:
    """Normalize date strings to ISO YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.match(r"(\d{4})\.\s*(\d{1,2})\.\s*(\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return raw


# ---------------------------------------------------------------------------
# List page parsing
# ---------------------------------------------------------------------------

def _parse_list_page(raw):
    """Parse a list page; return item dicts with nttId + partial metadata."""
    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] List page parse error: {exc}")
        return []

    items = []
    for row in soup.select("table tbody tr"):
        subj_td = row.select_one('td[data-cell-header="제목"]')
        if not subj_td:
            continue
        a_tag = subj_td.find("a", href=True)
        if not a_tag:
            continue
        m = re.search(r"nttId=([A-Za-z0-9]+)", a_tag["href"])
        if not m:
            continue
        ntt_id = m.group(1)

        title = a_tag.get_text(strip=True)
        title = html.unescape(title) if title else ""

        no_td = row.select_one('td[data-cell-header="번호"]')
        post_number = no_td.get_text(strip=True) if no_td else ""
        if not re.match(r"^\d+$", post_number or ""):
            post_number = ntt_id

        author_td = row.select_one('td[data-cell-header="작성자"]')
        department = author_td.get_text(strip=True) if author_td else ""

        date_td = row.select_one('td[data-cell-header="등록일"]')
        date_raw = date_td.get_text(strip=True) if date_td else ""

        items.append({
            "ntt_id": ntt_id,
            "title_from_list": title,
            "post_number": post_number,
            "department_from_list": department,
            "date_raw": date_raw,
        })

    return items


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------

def _parse_detail_page(raw, ntt_id: str):
    """Parse a BBS detail page.

    Returns a dict with title, department, date_raw, published_date,
    content_text, attachments, pdf_url, original_filename. Returns None on
    unrecoverable failure.
    """
    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] Detail page BS4 error nttId={ntt_id}: {exc}")
        return None

    # -- Title -----------------------------------------------------------
    title = ""
    tit_el = soup.select_one("h2.bbs--view--tit") or soup.select_one(".bbs--view--tit")
    if tit_el:
        for badge in tit_el.select(".ir-bbs-new, .ir"):
            badge.extract()
        title = tit_el.get_text(strip=True)
    if not title:
        pt = soup.find("title")
        if pt:
            raw_title = pt.get_text(strip=True)
            raw_title = re.sub(r"\s*[|\-–].*$", "", raw_title).strip()
            title = raw_title if len(raw_title) > 5 else ""

    # -- Meta (작성자/department, 등록일/date) ----------------------------
    department = ""
    date_raw = ""
    header = soup.select_one(".bbs--view--header") or soup
    for span in header.select("span"):
        i_tag = span.find("i")
        if not i_tag:
            continue
        label = i_tag.get_text(strip=True)
        value = span.get_text(strip=True).replace(label, "", 1).strip()
        if label == "작성자":
            department = value
        elif label == "등록일":
            date_raw = value

    # -- Content -----------------------------------------------------------
    content_text = ""
    cont_el = soup.select_one(".bbs--view--content")
    if cont_el:
        content_text = cont_el.get_text(separator="\n", strip=True)
        content_text = re.sub(r"\n{2,}", "\n", content_text).strip()
    if not content_text:
        cont_el = soup.select_one(".bbs--view--cont")
        if cont_el:
            content_text = cont_el.get_text(separator="\n", strip=True)

    # -- Attachments ---------------------------------------------------------
    attachments = []  # list of (atch_file_id, file_sn, filename)
    for a_el in soup.select("a[href*='fn_egov_downFile']"):
        href = a_el.get("href", "")
        m = re.search(r"fn_egov_downFile\('([^']+)'\s*,\s*'([^']+)'\)", href)
        if not m:
            continue
        atch_file_id, file_sn = m.group(1), m.group(2)
        fn_text = a_el.get_text(strip=True)
        fn_text = re.sub(r"\s*\[[0-9.,]+\s*[KMG]?B\]\s*$", "", fn_text).strip()
        if fn_text:
            attachments.append({"atch_file_id": atch_file_id, "file_sn": file_sn, "filename": fn_text})

    pdf_url = None
    original_filename = None
    for att in attachments:
        if att["filename"].lower().endswith(".pdf"):
            pdf_url = f"{_FILE_URL}?atchFileId={att['atch_file_id']}&fileSn={att['file_sn']}"
            original_filename = att["filename"]
            break

    return {
        "title": title,
        "department": department,
        "date_raw": date_raw,
        "published_date": _parse_date(date_raw),
        "content_text": content_text,
        "attachments": attachments,
        "pdf_url": pdf_url,
        "original_filename": original_filename,
    }


def _build_abstract(content_text, attachments, title, department, date_raw):
    """Build a rich abstract from body text, attachment names, and metadata."""
    parts = []
    if content_text:
        parts.append(content_text)
    if attachments:
        names = "; ".join(a["filename"] for a in attachments)
        parts.append(f"첨부파일: {names}")

    abstract = "\n".join(parts)

    if len(abstract) < _SAVE_ABSTRACT_LEN:
        header_parts = []
        if title:
            header_parts.append(f"제목: {title}")
        if department:
            header_parts.append(f"작성자: {department}")
        if date_raw:
            header_parts.append(f"등록일: {date_raw}")
        header = "\n".join(header_parts)
        if header:
            abstract = header + ("\n\n" if abstract else "") + abstract

    return abstract.strip()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class SejongGoKrBbsCrawler(BaseCrawler):
    """세종특별자치시 보도자료 (R0079) BBS crawler."""

    site_id = _SITE_ID
    site_name = "Custom: sejong-go-kr-bbs"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Walk BBS pages, fetch each detail page, and save records.

        Parameters
        ----------
        limit:
            Max records to save. ``None`` means unlimited.
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):

            elapsed = time.time() - start_time
            if elapsed > _CRAWL_BUDGET_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget ({_CRAWL_BUDGET_SECS}s) exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")

            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            list_url = f"{_LIST_URL}?pageIndex={page}"
            list_raw = _curl_get(list_url)
            if not list_raw:
                print(f"[{_SITE_ID}] Failed to fetch list page {page}. Stopping.")
                break

            items = _parse_list_page(list_raw)
            if not items:
                print(f"[{_SITE_ID}] No items found on page {page}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                ntt_id = item["ntt_id"]
                detail_url = f"{_VIEW_URL}?nttId={ntt_id}&mno=sub02_0401&cmsNoStr=&pageIndex=1&kind="

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(detail_url, referer=list_url)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] Failed to fetch nttId={ntt_id}. Skipping.")
                        continue

                    detail = _parse_detail_page(detail_raw, ntt_id)
                    if detail is None:
                        print(f"[{_SITE_ID}] Parse error for nttId={ntt_id}. Skipping.")
                        continue

                    title = detail["title"] or item.get("title_from_list", "")
                    if not title:
                        print(f"[{_SITE_ID}] No title for nttId={ntt_id}. Skipping.")
                        continue

                    department = detail.get("department") or item.get("department_from_list", "")
                    date_raw = detail.get("date_raw") or item.get("date_raw", "")
                    published_date = detail.get("published_date") or _parse_date(item.get("date_raw", ""))

                    abstract = _build_abstract(
                        content_text=detail.get("content_text", ""),
                        attachments=detail.get("attachments", []),
                        title=title,
                        department=department,
                        date_raw=date_raw,
                    )

                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        print(f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars) for nttId={ntt_id}. Skipping.")
                        continue

                    attachments = detail.get("attachments", [])
                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": ntt_id,
                        "post_number": item.get("post_number") or ntt_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": _parse_date(item.get("date_raw", "")) or published_date,
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "department": department or None,
                        "publisher": "세종특별자치시",
                        "authors": None,
                        "keywords": None,
                        "doi": None,
                        "category": "보도자료",
                        "metadata": json.dumps({
                            "nttId": ntt_id,
                            "posted_date": item.get("date_raw", ""),
                            "department": department,
                            "originalFilename": detail.get("original_filename"),
                            "journal_raw": None,
                            "series": None,
                            "volume": None,
                            "issue": None,
                            "attachments": attachments,
                        }, ensure_ascii=False),
                    })

                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item nttId={ntt_id} failed: {exc}")
                    continue

            if new_on_page == 0 and page > 1:
                print(f"[{_SITE_ID}] Page {page}: all items already seen. Done.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
