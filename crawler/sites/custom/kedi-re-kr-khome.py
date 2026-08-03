# -*- coding: utf-8 -*-
"""Crawler for KEDI 연구보고서 (한국교육개발원 Research Publications).

Starting page:
    https://www.kedi.re.kr/khome/main/research/listPubForm.do

Discovered endpoints:
    list   POST /khome/main/research/listPubForm.do  (currentPage=N)
    detail POST /khome/main/research/selectPubForm.do (plNum0=ID)
    pdf    POST /khome/main/research/downloadPubFileAction.do (form submit)

Each list row has: title (with plNum0 from selectPubFormFn), category, date,
author (연구책임자), views, file icon.  Abstract lives in detail page's
<div class="reportCont">.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


_BASE_URL = "https://www.kedi.re.kr"
_LIST_URL = f"{_BASE_URL}/khome/main/research/listPubForm.do"
_DETAIL_URL = f"{_BASE_URL}/khome/main/research/selectPubForm.do"
_DOWNLOAD_URL = f"{_BASE_URL}/khome/main/research/downloadPubFileAction.do"

_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_BUDGET_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 100  # test requires LENGTH(abstract) >= 100

_BACKOFFS = (1, 3, 9)
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _decode(raw: bytes | None) -> str:
    """Decode response bytes tolerating mixed Korean encodings."""
    if not raw:
        return ""
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _soup(html: str):
    """Construct BeautifulSoup with html5lib → lxml → html.parser fallback."""
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        print(f"[kedi-re-kr-khome] BeautifulSoup import failed: {exc}")
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _text(node: Any, separator: str = " ") -> str:
    if node is None:
        return ""
    if hasattr(node, "get_text"):
        value = node.get_text(separator=separator, strip=True)
    else:
        value = re.sub(r"<[^>]+>", " ", str(node))
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def _norm_date(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.strip()
    m = re.search(r"(19|20)\d{2}[.\-/]\d{1,2}[.\-/]\d{1,2}", raw)
    if m:
        parts = re.split(r"[.\-/]", m.group(0))
        if len(parts) == 3:
            return f"{int(parts[0]):04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
    m = re.search(r"(19|20)\d{6}", raw)
    if m:
        s = m.group(0)
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def _quoted_args(onclick: str | None) -> list[str]:
    """Extract quoted JS call arguments from an onclick string."""
    if not onclick:
        return []
    args: list[str] = []
    for single, double in re.findall(
        r"'((?:\\'|[^'])*)'|\"((?:\\\"|[^\"])*)\"", onclick
    ):
        value = single if single != "" else double
        args.append(value.replace("\\'", "'").replace('\\"', '"'))
    return args


def _curl(
    url: str,
    *,
    method: str = "GET",
    data: dict[str, str] | None = None,
    referer: str | None = None,
    item_label: str | None = None,
) -> bytes | None:
    """Fetch via curl with TLS workaround and 3-attempt exponential retry."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    if method.upper() == "POST":
        cmd += [
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
        ]
        for key, value in (data or {}).items():
            cmd += ["--data-urlencode", f"{key}={value}"]
    cmd.append(url)

    label = item_label or url
    for attempt, wait in enumerate(_BACKOFFS, start=1):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            if result.returncode == 0 and result.stdout:
                return result.stdout
            msg = result.stderr.decode("utf-8", errors="replace").strip()
            print(
                f"[kedi-re-kr-khome] curl attempt {attempt}/3 failed for {label}: "
                f"rc={result.returncode} {msg[:120]}"
            )
        except Exception as exc:
            print(f"[kedi-re-kr-khome] curl attempt {attempt}/3 failed for {label}: {exc}")
        if attempt < 3:
            time.sleep(wait)
    print(f"[kedi-re-kr-khome] curl failed after 3 attempts for {label}")
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KediReKrKhomeCrawler(BaseCrawler):
    """Crawler for 한국교육개발원 (KEDI) 연구보고서."""

    site_id = "kedi-re-kr-khome"
    site_name = "Custom: kedi-re-kr-khome"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        started_at = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if self._budget_reached(started_at, page):
                break
            if page % 10 == 0:
                print(f"[kedi-re-kr-khome] page {page}: saved {saved}/{limit_str}")
            if page == _MAX_PAGES:
                print(f"[kedi-re-kr-khome] safety cap of {_MAX_PAGES} pages reached")

            list_html = self._fetch_list_page(page)
            if not list_html:
                print(f"[kedi-re-kr-khome] page {page}: list fetch failed, stopping")
                break

            items = self._parse_list_page(list_html, page)
            if not items:
                print(f"[kedi-re-kr-khome] page {page}: no items, stopping")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[kedi-re-kr-khome] page {page}: all items already seen, stopping")
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                if self._budget_reached(started_at, page):
                    return saved

                seen_urls.add(item["url"])
                label = item.get("plNum0", "?")

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_and_parse_detail(item)

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[kedi-re-kr-khome] item {label}: abstract too short "
                            f"({len(abstract)} chars), skipping"
                        )
                        continue

                    paper = self._build_paper(item, detail)
                    self._save_paper(paper)
                    saved += 1
                    print(f"[kedi-re-kr-khome] saved {saved}/{limit_str}: {paper['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[kedi-re-kr-khome] item {label} failed: {exc}")
                    continue

        print(f"[kedi-re-kr-khome] done. total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> str | None:
        raw = _curl(
            _LIST_URL,
            method="POST",
            referer=_LIST_URL,
            item_label=f"list page {page}",
            data={
                "maxLinks": "10",
                "maxResults": str(_PAGE_SIZE),
                "currentPage": str(page),
                "tabGb": "1",
                "sort": "",
                "sortCol": "",
                "sortOp": "",
                "srchStartYear": "1972",
                "srchStartMonth": "01",
                "srchEndYear": "2026",
                "srchEndMonth": "12",
            },
        )
        return _decode(raw) if raw else None

    def _parse_list_page(self, html: str, page: int) -> list[dict[str, Any]]:
        so = _soup(html)
        if so is None:
            print(f"[kedi-re-kr-khome] page {page}: HTML parse failed")
            return []

        items: list[dict[str, Any]] = []
        for tr in so.select("table.colTbl tbody tr"):
            # Skip "no data" rows
            if tr.select_one("td.nolist"):
                continue

            # Find the link triggering selectPubFormFn
            link = tr.find("a", onclick=re.compile(r"selectPubFormFn"))
            if not link:
                continue
            onclick = link.get("onclick", "")
            m = re.search(r"selectPubFormFn\('(\d+)'\)", onclick)
            if not m:
                # Fallback: checkid hidden input
                hidden = tr.find("input", id=re.compile(r"checkid_\d+"))
                if hidden:
                    m = re.search(r"checkid_(\d+)", hidden.get("id", ""))
            if not m:
                continue

            pl_num0 = m.group(1)
            cells = tr.find_all("td", recursive=False)

            title = _text(link) or f"KEDI report {pl_num0}"
            category = _text(cells[1]) if len(cells) > 1 else ""
            raw_date = _text(cells[2]) if len(cells) > 2 else ""
            author = _text(cells[3]) if len(cells) > 3 else ""

            items.append({
                "plNum0": pl_num0,
                "external_id": pl_num0,
                "post_number": pl_num0,
                "title": title,
                "category": category,
                "raw_date": raw_date,
                "published_date": _norm_date(raw_date),
                "author": author,
                "url": f"{_BASE_URL}/khome/main/research/selectPubForm.do?plNum0={pl_num0}",
                "list_page": page,
            })
        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_and_parse_detail(self, item: dict[str, Any]) -> dict[str, Any]:
        pl_num0 = item["plNum0"]
        raw = _curl(
            _DETAIL_URL,
            method="POST",
            referer=_LIST_URL,
            item_label=f"detail {pl_num0}",
            data={
                "maxLinks": "10",
                "maxResults": str(_PAGE_SIZE),
                "currentPage": "1",
                "plNum0": pl_num0,
                "tabGb": "1",
                "sort": "",
                "sortCol": "",
                "sortOp": "",
                "srchStartYear": "1972",
                "srchStartMonth": "01",
                "srchEndYear": "2026",
                "srchEndMonth": "12",
            },
        )
        if not raw:
            raise RuntimeError(f"detail fetch failed for {pl_num0}")
        html = _decode(raw)
        return self._parse_detail_page(html, item)

    def _parse_detail_page(self, html: str, item: dict[str, Any]) -> dict[str, Any]:
        so = _soup(html)
        if so is None:
            raise RuntimeError("detail HTML parse failed (BeautifulSoup returned None)")

        # Title
        title = _text(so.select_one("h4.boardTit")) or item.get("title") or ""

        # Abstract from reportCont (table-of-contents style pages will be short → skipped)
        abstract = _text(
            so.select_one("div.reportSummary div.reportCont"), separator="\n"
        )
        if not abstract:
            abstract = _text(so.select_one("div.reportSummary"), separator="\n")

        # Report info (보고서번호, 페이지, ISBN, 출판유형)
        report_info: dict[str, str] = {}
        for li in so.select("ul.reportInfo li"):
            key_node = li.find("span", class_="tit")
            if not key_node:
                continue
            key = _text(key_node).strip().rstrip(":").strip()
            key_node.extract()
            report_info[key] = _text(li)

        report_number = report_info.get("보고서번호")
        isbn = report_info.get("ISBN") or None
        page_count = report_info.get("페이지") or None

        # PDF params from downloadAction('orig.pdf','stored.pdf','A00/000301','P')
        dl_link = so.find("a", onclick=re.compile(r"downloadAction"))
        dl_args = _quoted_args(dl_link.get("onclick", "") if dl_link else "")
        orig_name   = dl_args[0] if len(dl_args) > 0 else None
        stored_name = dl_args[1] if len(dl_args) > 1 else None
        file_path   = dl_args[2] if len(dl_args) > 2 else None
        pdf_url     = _DOWNLOAD_URL if (orig_name or stored_name) else None

        return {
            "title": title,
            "abstract": abstract,
            "report_number": report_number,
            "isbn": isbn,
            "page_count": page_count,
            "report_info": report_info,
            "original_filename": orig_name or None,
            "pdf_stored_name": stored_name,
            "pdf_path_part": file_path,
            "pdf_url": pdf_url,
        }

    # ------------------------------------------------------------------
    # Paper assembly
    # ------------------------------------------------------------------

    def _build_paper(self, item: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
        pl_num0 = item["plNum0"]
        title = detail.get("title") or item["title"]
        published_date = item.get("published_date")
        report_number = detail.get("report_number")

        metadata = {
            "posted_date": item.get("raw_date"),
            "originalFilename": detail.get("original_filename"),
            "plNum0": pl_num0,
            "report_number": report_number,
            "isbn": detail.get("isbn"),
            "page_count": detail.get("page_count"),
            "pdf_stored_name": detail.get("pdf_stored_name"),
            "pdf_path_part": detail.get("pdf_path_part"),
            "category_raw": item.get("category"),
            "list_page": item.get("list_page"),
        }

        return {
            "id": f"{self.site_id}:{pl_num0}",
            "site_id": self.site_id,
            "external_id": pl_num0,
            "post_number": pl_num0,
            "title": title,
            "abstract": detail.get("abstract") or "",
            "published_date": published_date,
            "listed_date": published_date,
            "posted_date": published_date,
            "authors": item.get("author") or None,
            "publisher": "한국교육개발원",
            "department": None,
            "journal": None,
            "url": item["url"],
            "pdf_url": detail.get("pdf_url"),
            "keywords": None,
            "category": item.get("category") or None,
            "doi": None,
            "original_filename": detail.get("original_filename"),
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _budget_reached(self, started_at: float, page: int) -> bool:
        elapsed = time.time() - started_at
        if elapsed >= (_WALL_BUDGET_SEC - 5):
            print(
                f"[kedi-re-kr-khome] approaching 25-minute wall-clock budget "
                f"at page {page}, stopping cleanly"
            )
            return True
        return False
