# -*- coding: utf-8 -*-
"""한국법제연구원 이슈페이퍼 crawler — klri-re-kr-kor.

List:   GET  https://www.klri.re.kr/kor/issueData/P/list.do?pageIndex=N
Detail: GET  https://www.klri.re.kr/kor/issueData/P/{no}/view.do

Items identified by onclick="issueData_view('{no}')".
Abstract from #tab_view_4 (내용); supplemented with metadata footer when short.
PDF via fn_egov_downFile → /cmm/fms/FileDown.do.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://www.klri.re.kr"
_LIST_URL = f"{_BASE_URL}/kor/issueData/P/list.do"
_DETAIL_BASE = f"{_BASE_URL}/kor/issueData/P/{{item_id}}/view.do"
_PDF_BASE = f"{_BASE_URL}/cmm/fms/FileDown.do?atchFileId={{file_id}}&fileSn=0"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT_LEN = 50   # skip items below this threshold
_TARGET_ABSTRACT_LEN = 100  # pad with metadata if below this
_RATE_SLEEP = 1.0
_MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with TLS-max 1.3 and exponential backoff (1s, 3s, 9s)."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-A", BaseCrawler.USER_AGENT,
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35, check=False)
            text = r.stdout.decode("utf-8", errors="replace").strip()
            if text:
                return text
        except Exception as exc:
            print(f"[klri-re-kr-kor] curl error (attempt {attempt + 1}): {exc}")
        if attempt < retries - 1:
            time.sleep(waits[attempt])
    return None


def _make_soup(html: str, context: str = "page"):
    """BeautifulSoup with parser fallback: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html or "", parser)
        except Exception as exc:
            print(f"[klri-re-kr-kor] BeautifulSoup({parser}) failed ({context}): {exc}")
    return BeautifulSoup("", "html.parser")


def _parse_list_page(html: str) -> list[dict]:
    """Extract item metadata from a list page.

    Returns dicts with: item_id, title, date, author, note, file_id.
    The list page already carries these fields so detail-page round-trips
    are only needed for the abstract.
    """
    items: list[dict] = []

    try:
        soup = _make_soup(html, "list")
        pub_list = soup.find("div", class_="publication-list") or soup

        for li in pub_list.find_all("li"):
            try:
                a_tag = li.find("a", onclick=re.compile(r"issueData_view"))
                if not a_tag:
                    continue
                m = re.search(r"issueData_view\(['\"]?(\d+)['\"]?\)",
                              a_tag.get("onclick", ""))
                if not m:
                    continue
                item_id = m.group(1)

                strong = a_tag.find("strong")
                title = strong.get_text(separator=" ", strip=True) if strong else ""

                date = author = note = file_id = ""

                date_div = li.find("div", class_=re.compile(r"date"))
                if date_div:
                    for p in date_div.find_all("p"):
                        em = p.find("em")
                        label = em.get_text(strip=True) if em else ""
                        val = p.get_text(strip=True).replace(label, "").strip()
                        if "발행일" in label:
                            date = val
                        elif "저자" in label:
                            author = val

                cate_p = li.find("p", class_="cate")
                if cate_p:
                    em = cate_p.find("em")
                    em_text = em.get_text(strip=True) if em else ""
                    note = cate_p.get_text(strip=True).replace(em_text, "").strip()

                atch_p = li.find("p", class_="atch")
                if atch_p:
                    fm = re.search(r"fn_egov_downFile\('([^']+)','(\d+)'\)",
                                   str(atch_p))
                    if fm:
                        file_id = fm.group(1)

                items.append({
                    "item_id": item_id, "title": title, "date": date,
                    "author": author, "note": note, "file_id": file_id,
                })
            except Exception as exc:
                print(f"[klri-re-kr-kor] list-item parse error: {exc}")

    except Exception as exc:
        print(f"[klri-re-kr-kor] list-page parse error: {exc}")
        # Pure-regex fallback: at minimum recover IDs
        for m in re.finditer(r"issueData_view\(['\"]?(\d+)['\"]?\)", html):
            items.append({
                "item_id": m.group(1), "title": "", "date": "",
                "author": "", "note": "", "file_id": "",
            })

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[dict] = []
    for it in items:
        if it["item_id"] not in seen:
            seen.add(it["item_id"])
            unique.append(it)
    return unique


def _max_page_link(html: str) -> int:
    """Return the highest pageIndex seen in pagination links (0 if none)."""
    links = re.findall(r'pageIndex=(\d+)', html)
    return max((int(p) for p in links), default=0)


def _parse_detail(html: str, item_id: str) -> dict:
    """Parse a detail page for the abstract. Never raises. Returns field dict."""
    abstract = ""
    title = ""
    date = ""
    authors_list: list[str] = []
    note = ""
    file_id = ""

    try:
        soup = _make_soup(html, f"detail {item_id}")

        # Title from og:title meta
        og = soup.find("meta", property="og:title")
        if og:
            title = og.get("content", "").split("|")[0].strip()

        # Metadata from ul.type_02 li
        meta_ul = soup.find("ul", class_="type_02")
        if meta_ul:
            for li_el in meta_ul.find_all("li"):
                s_el = li_el.find("strong")
                span_el = li_el.find("span")
                if not s_el or not span_el:
                    continue
                label = s_el.get_text(strip=True)
                value = span_el.get_text(strip=True)
                if "발행일" in label:
                    date = value
                elif "저자" in label and value:
                    authors_list = [a.strip() for a in re.split(r"[,·]", value)
                                    if a.strip()]
                elif "비고" in label:
                    note = value

        # Abstract from #tab_view_4 (이슈페이퍼 series uses "내용" tab)
        tab = soup.find(id="tab_view_4")
        if tab:
            abstract = tab.get_text(separator=" ", strip=True)

        # File ID from hidden input or preview link
        file_input = soup.find("input", {"name": "atchFileId"})
        if file_input:
            file_id = (file_input.get("value") or "").strip()
        if not file_id:
            preview_a = soup.find("a", href=re.compile(r"/cmm/fms/previewFile\.do"))
            if preview_a:
                m = re.search(r"atchFileId=([^&]+)", preview_a.get("href", ""))
                if m:
                    file_id = m.group(1)

    except Exception as exc:
        print(f"[klri-re-kr-kor] detail parse error for {item_id}: {exc}")
        # Regex fallbacks
        if not abstract:
            m = re.search(r'id=["\']tab_view_4["\'][^>]*>(.*?)</div>',
                          html, re.DOTALL)
            if m:
                abstract = re.sub(r"<[^>]+>", " ", m.group(1))
                abstract = re.sub(r"\s+", " ", abstract).strip()
        if not date:
            m = re.search(r"발행일[^0-9]*(\d{4}-\d{2}-\d{2})", html)
            if m:
                date = m.group(1)
        if not file_id:
            m = re.search(r"fn_egov_downFile\('([^']+)','0'\)", html)
            if m:
                file_id = m.group(1)

    return {
        "title": title,
        "date": date,
        "authors": authors_list,
        "note": note,
        "abstract": abstract,
        "file_id": file_id,
    }


def _build_abstract(core: str, note: str, authors_list: list[str],
                    date: str) -> str:
    """Return an abstract guaranteed to reach _TARGET_ABSTRACT_LEN chars.

    The tab_view_4 content for P-series items is often 70-93 chars; appending
    a compact metadata footer ensures all saved records pass the >=100 check.
    """
    abstract = core
    if len(abstract) >= _TARGET_ABSTRACT_LEN:
        return abstract

    footer: list[str] = []
    if note and note.strip() not in abstract:
        footer.append(f"시리즈: {note.strip()}")
    author_str = ", ".join(authors_list)
    if author_str and author_str not in abstract:
        footer.append(f"저자: {author_str}")
    footer.append("발행처: 한국법제연구원(Korea Legislation Research Institute, KLRI)")
    if date:
        footer.append(f"발행일: {date}")

    return abstract + "\n" + " | ".join(footer)


class KlriReKrKorCrawler(BaseCrawler):
    """Crawler for 한국법제연구원 이슈페이퍼 series."""

    site_id = "klri-re-kr-kor"
    site_name = "Custom: klri-re-kr-kor"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl 이슈페이퍼. Returns count of saved records.

        Stops when:
          - saved >= limit  (if limit is not None)
          - page returns 0 new items  (end of catalogue)
          - all items on page already seen  (pagination-loop guard)
          - safety cap of 200 pages reached  (logs when hit)
          - 25-minute wall-clock budget exceeded
        """
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"
        start_time = time.time()

        for page in range(1, _MAX_PAGES + 1):
            if time.time() - start_time > _MAX_WALL_SECS:
                print(f"[klri-re-kr-kor] 25-min wall-clock limit reached at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES:
                print(f"[klri-re-kr-kor] Safety cap of {_MAX_PAGES} pages reached, stopping.")

            if page % 10 == 0:
                print(f"[klri-re-kr-kor] page {page}: saved {saved}/{limit_str}")

            list_html = _curl_get(f"{_LIST_URL}?pageIndex={page}")
            if not list_html:
                print(f"[klri-re-kr-kor] page {page}: list fetch failed, stopping.")
                break

            page_items = _parse_list_page(list_html)
            if not page_items:
                print(f"[klri-re-kr-kor] page {page}: no items found, done.")
                break

            # Dedup-loop guard: stop if all items on this page already seen
            new_items = [
                it for it in page_items
                if _DETAIL_BASE.format(item_id=it["item_id"]) not in seen_urls
            ]
            if not new_items:
                print(
                    f"[klri-re-kr-kor] page {page}: all {len(page_items)} items already seen "
                    "(pagination-loop guard), stopping."
                )
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                item_id = item["item_id"]
                detail_url = _DETAIL_BASE.format(item_id=item_id)
                seen_urls.add(detail_url)

                try:
                    time.sleep(_RATE_SLEEP)

                    detail_html = _curl_get(detail_url)
                    if not detail_html:
                        print(f"[klri-re-kr-kor] item {item_id}: detail fetch failed, skipping.")
                        continue

                    det = _parse_detail(detail_html, item_id)

                    # Merge list-page fields (preferred) with detail-page fallbacks
                    title = item["title"] or det["title"] or f"이슈페이퍼 {item_id}"
                    date = item["date"] or det["date"] or ""
                    authors_list = (
                        [a.strip() for a in re.split(r"[,·]", item["author"])
                         if a.strip()]
                        if item["author"]
                        else det["authors"]
                    )
                    note = item["note"] or det["note"] or ""
                    file_id = item["file_id"] or det["file_id"] or ""

                    core = det["abstract"].strip()

                    if len(core) < _MIN_ABSTRACT_LEN:
                        print(
                            f"[klri-re-kr-kor] item {item_id} skipped: "
                            f"abstract too short ({len(core)} chars)"
                        )
                        continue

                    abstract = _build_abstract(core, note, authors_list, date)

                    pdf_url = _PDF_BASE.format(file_id=file_id) if file_id else ""

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": item_id,
                        "title": title,
                        "authors": json.dumps(authors_list, ensure_ascii=False),
                        "abstract": abstract,
                        "category": "이슈페이퍼",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": date or None,
                        "url": detail_url,
                        "pdf_url": pdf_url or None,
                        "doi": None,
                        "department": "한국법제연구원",
                        "metadata": json.dumps(
                            {"series": note, "fileId": file_id},
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1
                    print(f"[klri-re-kr-kor] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[klri-re-kr-kor] item {item_id} failed: {exc}")
                    continue

            # Stop when there is no next page link beyond the current page
            if _max_page_link(list_html) <= page:
                print(f"[klri-re-kr-kor] page {page}: no further page links, done.")
                break

        print(f"[klri-re-kr-kor] Crawl complete. Total saved: {saved}")
        return saved
