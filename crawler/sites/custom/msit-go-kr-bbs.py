# -*- coding: utf-8 -*-
"""과학기술정보통신부 (MSIT) BBS crawler — 연구개발·미래인재 자료 (mId=244).

Starting URL: https://www.msit.go.kr/bbs/list.do?sCode=user&mPid=243&mId=244

List page structure:
  - nttSeqNos in onclick="fn_detail(N);" on .board_list a elements
  - Titles injected via inline JS: sHtml+= unescape('TITLE');
                                    $('#td_'+'NTT_SJ'+'_N').html(sHtml);
  - Dates:  $('#td_'+'REG_DT'+'_N').html('2026. 1. 2');

Detail page structure:
  - Title: h2 in .view_head
  - Meta: dl.tit_con dt/dd pairs (부서, 담당자, 작성일)
  - Content: #cont-wrap.view_cont or .texteditor-area-inner
  - Attachments: fn_download(atchFileNo, fileOrd, ext) in ul.down_file_new a
  - Download URL: POST /ssm/file/fileDown.do?atchFileNo=X&fileOrd=Y
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

_SITE_ID = "msit-go-kr-bbs"
_BASE_URL = "https://www.msit.go.kr"
_LIST_URL = f"{_BASE_URL}/bbs/list.do"
_VIEW_URL = f"{_BASE_URL}/bbs/view.do"
_FILE_URL = f"{_BASE_URL}/ssm/file/fileDown.do"
_BBS_SEQ_NO = "65"
_MID = "244"
_MPID = "243"
_SCODE = "user"
_REFERER_LIST = (
    f"{_BASE_URL}/bbs/list.do?sCode={_SCODE}&mPid={_MPID}&mId={_MID}"
)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_MAX_PAGES = 200
_CRAWL_BUDGET_SECS = 25 * 60  # 25 minutes wall-clock cap
_MIN_ABSTRACT_LEN = 50         # skip items below this
_SAVE_ABSTRACT_LEN = 100       # pad with metadata if below this


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _curl_get(url: str, referer: str = _REFERER_LIST, timeout: int = 30) -> bytes | None:
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
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            if result.returncode == 0 and result.stdout and len(result.stdout) > 200:
                return result.stdout
            wait = 3 ** attempt
            if attempt < 2:
                print(f"[{_SITE_ID}] curl small/empty (attempt {attempt + 1}/3), "
                      f"retry in {wait}s...")
                time.sleep(wait)
        except Exception as exc:
            wait = 3 ** attempt
            if attempt < 2:
                print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/3): {exc}, "
                      f"retry in {wait}s...")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after 3 attempts: {exc}")
    return None


# ---------------------------------------------------------------------------
# HTML parsing helpers
# ---------------------------------------------------------------------------

def _make_soup(raw: bytes):
    """BeautifulSoup with html5lib → lxml → html.parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    raise RuntimeError("No suitable HTML parser available")


def _strip_html(text: str) -> str:
    """Strip tags, convert <br> to newline, decode common HTML entities."""
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = re.sub(r"&#\d+;", "", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def _parse_date(raw: str) -> str:
    """Normalize date strings to ISO YYYY-MM-DD.

    Handles:
      '2025. 12. 17'  → '2025-12-17'
      '2025-12-17'    → '2025-12-17'
    """
    if not raw:
        return ""
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

def _parse_list_page(raw: bytes) -> list[dict]:
    """Parse list page; return item dicts with nttSeqNo + partial metadata.

    The MSIT BBS renders empty div placeholders in HTML and populates them
    via inline JavaScript. nttSeqNos come from onclick="fn_detail(N);";
    titles and dates are extracted from the inline JS with regex.
    """
    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] List page parse error: {exc}")
        return []

    board_list = soup.select_one(".board_list")
    if not board_list:
        return []

    # Extract nttSeqNos from onclick attrs — preserve order, deduplicate
    ntt_ids: list[str] = []
    seen_ids: set[str] = set()
    for a_tag in board_list.find_all("a", onclick=True):
        m = re.search(r"fn_detail\((\d+)\)", a_tag.get("onclick", ""))
        if m:
            nid = m.group(1)
            if nid not in seen_ids:
                seen_ids.add(nid)
                ntt_ids.append(nid)

    if not ntt_ids:
        return []

    text = raw.decode("utf-8", errors="replace")

    items: list[dict] = []
    for idx, ntt_id in enumerate(ntt_ids):
        item: dict = {"ntt_seq_no": ntt_id}

        # Post number: $('#td_'+'NO'+'_IDX').html('NUMBER');
        m = re.search(
            r"\$\('#td_'\+'NO'\+'_" + str(idx) + r"'\)\.html\('([^']+)'\)",
            text,
        )
        if m:
            v = m.group(1).strip()
            item["post_number"] = v if re.match(r"^\d+$", v) else ntt_id
        else:
            item["post_number"] = ntt_id

        # Title: last unescape('LITERAL') before $('#td_'+'NTT_SJ'+'_IDX').html(sHtml);
        marker = f"$('#td_'+'NTT_SJ'+'_{idx}').html(sHtml);"
        marker_pos = text.find(marker)
        if marker_pos > 0:
            block = text[:marker_pos]
            title_matches = re.findall(r"unescape\('([^']+)'\)", block)
            raw_title = title_matches[-1] if title_matches else ""
            item["title_from_list"] = html.unescape(raw_title) if raw_title else ""
        else:
            item["title_from_list"] = ""

        # Date: $('#td_'+'REG_DT'+'_IDX').html('2025. 12. 17');
        m = re.search(
            r"\$\('#td_'\+'REG_DT'\+'_" + str(idx) + r"'\)\.html\('([^']+)'\)",
            text,
        )
        item["date_raw"] = m.group(1).strip() if m else ""

        items.append(item)

    return items


# ---------------------------------------------------------------------------
# Detail page parsing
# ---------------------------------------------------------------------------

def _parse_detail_page(raw: bytes, ntt_seq_no: str) -> dict | None:
    """Parse a BBS detail page.

    Returns a dict with: title, date_raw, published_date, department,
    contact_person, content_text, all_filenames, pdf_url, atch_file_no,
    file_ord, original_filename.  Returns None on unrecoverable failure.
    """
    text = raw.decode("utf-8", errors="replace")

    try:
        soup = _make_soup(raw)
    except Exception as exc:
        print(f"[{_SITE_ID}] Detail page BS4 error nttSeqNo={ntt_seq_no}: {exc}")
        return None

    # Detect maintenance / error page
    page_text = soup.get_text(separator=" ")
    if "시스템 점검" in page_text and len(page_text) < 600:
        print(f"[{_SITE_ID}] Maintenance page for nttSeqNo={ntt_seq_no}")
        return None

    # -- Title ---------------------------------------------------------------
    title = ""
    view_head = soup.select_one(".view_head")
    if view_head:
        h2 = view_head.find("h2")
        if h2:
            title = h2.get_text(strip=True)
    if not title:
        pt = soup.find("title")
        if pt:
            raw_title = pt.get_text(strip=True)
            raw_title = re.sub(r"\s*[|–\-].*$", "", raw_title).strip()
            raw_title = re.sub(r"\s*\|.*$", "", raw_title).strip()
            title = raw_title if len(raw_title) > 5 else ""

    # -- Meta fields (부서, 담당자, 작성일) -----------------------------------
    meta_info: dict[str, str] = {}
    if view_head:
        for dl in view_head.select("dl.tit_con"):
            dt_el = dl.find("dt")
            dd_el = dl.find("dd")
            if dt_el and dd_el:
                key = dt_el.get_text(strip=True).strip()
                val = dd_el.get_text(strip=True)
                meta_info[key] = val

    date_raw = meta_info.get("작성일", "")
    department = meta_info.get("부서", "")
    contact_person = meta_info.get("담당자", "")

    # -- Content -------------------------------------------------------------
    content_text = ""
    inner = soup.select_one(".texteditor-area-inner")
    if inner:
        content_text = _strip_html(str(inner))
    if not content_text:
        cont = soup.select_one("#cont-wrap") or soup.select_one(".view_cont")
        if cont:
            content_text = _strip_html(str(cont))

    # -- Attachments ---------------------------------------------------------
    all_filenames: list[str] = []
    atch_file_no = ""
    file_ord = "1"
    original_filename = ""

    # Collect all fn_download calls: prefer PDF, fall back to first match
    dl_matches = re.findall(
        r"fn_download\('(\d+)',\s*'(\d+)',\s*'(\w+)'\)", text
    )

    # Collect display filenames from both down_file_new and down_file selectors
    for a_el in soup.select("ul.down_file_new a, .down_file li a"):
        fn = a_el.get_text(strip=True)
        if fn and "." in fn and len(fn) > 3:
            all_filenames.append(fn)

    # If no filenames found via soup, extract from JS onclick text
    if not all_filenames:
        # Try extracting filename from title attributes
        for a_el in soup.select("a[title='파일다운로드']"):
            fn = a_el.get_text(strip=True)
            if fn and "." in fn and len(fn) > 3:
                all_filenames.append(fn)

    # Choose file to reference: prefer PDF, else first match
    pdf_match = next((m for m in dl_matches if m[2].lower() == "pdf"), None)
    chosen = pdf_match or (dl_matches[0] if dl_matches else None)
    if chosen:
        atch_file_no, file_ord, chosen_ext = chosen
        # Pick filename that matches chosen ext if possible
        for fn in all_filenames:
            if fn.lower().endswith(f".{chosen_ext.lower()}"):
                original_filename = fn
                break
        if not original_filename and all_filenames:
            original_filename = all_filenames[0]

    # Construct download reference URL
    pdf_url: str | None = None
    if atch_file_no:
        pdf_url = f"{_FILE_URL}?atchFileNo={atch_file_no}&fileOrd={file_ord}"

    return {
        "title": title,
        "date_raw": date_raw,
        "published_date": _parse_date(date_raw),
        "department": department,
        "contact_person": contact_person,
        "content_text": content_text,
        "all_filenames": all_filenames,
        "pdf_url": pdf_url,
        "atch_file_no": atch_file_no,
        "file_ord": file_ord,
        "original_filename": original_filename,
    }


def _build_abstract(content_text: str, all_filenames: list[str],
                    title: str, department: str, date_raw: str,
                    contact_person: str) -> str:
    """Build a rich abstract from all available page content.

    Combines body text, attachment filenames, and structured metadata so the
    abstract is always substantive for any real page.  Returns empty string
    only when the page has no recoverable content at all.
    """
    parts: list[str] = []

    if content_text:
        parts.append(content_text)

    if all_filenames:
        parts.append("첨부파일: " + "; ".join(all_filenames))

    abstract = "\n".join(parts)

    # Pad with structured metadata if still short (gov BBS pages often have
    # one-line body text but rich filenames / metadata).
    if len(abstract) < _SAVE_ABSTRACT_LEN:
        header_parts: list[str] = []
        if title:
            header_parts.append(f"제목: {title}")
        if department:
            header_parts.append(f"부서: {department}")
        if date_raw:
            header_parts.append(f"작성일: {date_raw}")
        if contact_person:
            header_parts.append(f"담당자: {contact_person}")
        header = "\n".join(header_parts)
        if header:
            abstract = header + ("\n\n" if abstract else "") + abstract

    return abstract.strip()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MsitGoKrBbsCrawler(BaseCrawler):
    """과학기술정보통신부 BBS — 연구개발·미래인재 자료 (mId=244)."""

    site_id = "msit-go-kr-bbs"
    site_name = "Custom: msit-go-kr-bbs"
    base_url = "https://www.msit.go.kr"

    def crawl(self, limit=None):
        """Walk BBS pages, fetch each detail page, and save records.

        Parameters
        ----------
        limit:
            Max records to save.  None = unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):

            # Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > _CRAWL_BUDGET_SECS:
                print(f"[{_SITE_ID}] Wall-clock budget ({_CRAWL_BUDGET_SECS}s) exceeded. Stopping.")
                break

            # Limit reached
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page == _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # Fetch list page
            list_url = (
                f"{_LIST_URL}?sCode={_SCODE}&mId={_MID}"
                f"&mPid={_MPID}&pageIndex={page}"
            )
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

                ntt_seq_no = item["ntt_seq_no"]
                detail_url = (
                    f"{_VIEW_URL}?sCode={_SCODE}&mId={_MID}&mPid={_MPID}"
                    f"&bbsSeqNo={_BBS_SEQ_NO}&nttSeqNo={ntt_seq_no}"
                )

                # URL deduplication — catches paginator silent loopback
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = _curl_get(detail_url, referer=list_url)
                    if not detail_raw:
                        print(f"[{_SITE_ID}] Failed to fetch nttSeqNo={ntt_seq_no}. Skipping.")
                        continue

                    detail = _parse_detail_page(detail_raw, ntt_seq_no)
                    if detail is None:
                        print(f"[{_SITE_ID}] Parse error for nttSeqNo={ntt_seq_no}. Skipping.")
                        continue

                    title = detail["title"] or item.get("title_from_list", "")
                    if not title:
                        print(f"[{_SITE_ID}] No title for nttSeqNo={ntt_seq_no}. Skipping.")
                        continue

                    abstract = _build_abstract(
                        content_text=detail["content_text"],
                        all_filenames=detail.get("all_filenames", []),
                        title=title,
                        department=detail.get("department", ""),
                        date_raw=detail.get("date_raw", ""),
                        contact_person=detail.get("contact_person", ""),
                    )

                    if len(abstract) < _MIN_ABSTRACT_LEN:
                        print(
                            f"[{_SITE_ID}] Abstract too short ({len(abstract)} chars) "
                            f"for nttSeqNo={ntt_seq_no}. Skipping."
                        )
                        continue

                    date_raw = detail["date_raw"] or item.get("date_raw", "")
                    published_date = (
                        detail["published_date"] or _parse_date(item.get("date_raw", ""))
                    )

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": ntt_seq_no,
                        "post_number": item.get("post_number") or ntt_seq_no,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename") or None,
                        "department": detail.get("department") or "",
                        "publisher": "과학기술정보통신부",
                        "authors": detail.get("contact_person") or "",
                        "keywords": None,
                        "doi": None,
                        "category": "연구개발·미래인재",
                        "metadata": json.dumps({
                            "nttSeqNo": ntt_seq_no,
                            "bbsSeqNo": _BBS_SEQ_NO,
                            "posted_date": date_raw,
                            "contact_person": detail.get("contact_person"),
                            "department": detail.get("department"),
                            "atchFileNo": detail.get("atch_file_no"),
                            "fileOrd": detail.get("file_ord"),
                            "originalFilename": detail.get("original_filename"),
                            "all_filenames": detail.get("all_filenames", []),
                        }, ensure_ascii=False),
                    })

                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item nttSeqNo={ntt_seq_no} failed: {exc}")
                    continue

            # If every URL on this page was already seen → paginator looped back
            if new_on_page == 0 and page > 1:
                print(f"[{_SITE_ID}] Page {page}: all items already seen. Done.")
                break

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
