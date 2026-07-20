# -*- coding: utf-8 -*-
"""Crawler for GRI (경기연구원) research reports.

List:   https://www.gri.re.kr/web/contents/resreport.do?page=N
Detail: https://www.gri.re.kr/web/contents/resreport.do?schM=view&schProjectNo=X&schBookResultNo=Y
PDF:    POST https://library.gri.re.kr/download.do  (filename=PATH)

Each list <li> carries title/authors/category/year and PDF path.
The abstract lives in .middle_wrap on the detail page.
post_number = schBookResultNo (sequential descending integer).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse
from typing import Any

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class GriReKrWebCrawler(BaseCrawler):
    site_id = "gri-re-kr-web"
    site_name = "Custom: gri-re-kr-web"
    base_url = "https://www.gri.re.kr"

    _LIST_URL = "https://www.gri.re.kr/web/contents/resreport.do"
    _DOWNLOAD_BASE = "https://library.gri.re.kr/download.do"
    _PUBLISHER = "경기연구원"

    MAX_PAGES = 200
    _MAX_RUNTIME_S = 25 * 60
    _RUNTIME_GRACE_S = 30

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set[str] = set()
        limit_label = limit if limit is not None else "inf"
        t0 = time.monotonic()

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if self._budget_exhausted(t0):
                print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = f"{self._LIST_URL}?page={page}"
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] Empty response for list page {page}; stopping")
                break

            items = self._parse_list_items(raw)
            if not items:
                print(f"[{self.site_id}] No items on list page {page}; stopping")
                break

            new_count = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                detail_url = (
                    f"{self._LIST_URL}?schM=view"
                    f"&schProjectNo={item['schProjectNo']}"
                    f"&schBookResultNo={item['schBookResultNo']}"
                )
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_count += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail bookResultNo={item['schBookResultNo']}",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(item, detail_raw, detail_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] bookResultNo={item['schBookResultNo']} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item bookResultNo={item['schBookResultNo']} failed: {exc}")
                    continue

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: 0 new items; stopping")
                break

            page += 1
        else:
            print(f"[{self.site_id}] Safety cap reached at {self.MAX_PAGES} pages; stopping")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(
        self,
        url: str,
        *,
        context: str = "request",
        referer: str | None = None,
        timeout: int = 45,
    ) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        waits = [1, 3, 9]
        last_err = ""
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 5, check=False
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                last_err = (
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or f"curl exit {result.returncode}"
                )
            except subprocess.TimeoutExpired as exc:
                last_err = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_err = str(exc)

            if attempt < 2:
                w = waits[attempt]
                print(
                    f"[{self.site_id}] {context} failed "
                    f"(attempt {attempt + 1}/3): {last_err}; retrying in {w}s"
                )
                time.sleep(w)

        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_err}")
        return None

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str, *, context: str = "html") -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return BeautifulSoup("", "html.parser")

    def _parse_list_items(self, raw: str) -> list[dict]:
        """Parse list page into item dicts with basic metadata."""
        soup = self._make_soup(raw, context="list page")

        # Find the UL inside issue_table_wrap
        wrap = soup.select_one(".issue_table_wrap")
        if wrap:
            ul = wrap.find("ul")
            if ul:
                items = []
                for li in ul.find_all("li", recursive=False):
                    item = self._parse_list_li(li)
                    if item:
                        items.append(item)
                if items:
                    return items

        # Fallback: regex over raw HTML
        return self._parse_list_items_regex(raw)

    def _parse_list_li(self, li) -> dict | None:
        """Extract metadata from a single list <li> element."""
        # schProjectNo and schBookResultNo from fn_goView onclick
        onclick = ""
        for a in li.find_all("a"):
            oc = a.get("onclick", "")
            if "fn_goView" in oc:
                onclick = oc
                break

        if not onclick:
            return None

        m = re.search(r"fn_goView\('([^']+)',\s*'([^']+)'\)", onclick)
        if not m:
            return None

        proj_no = m.group(1)
        book_result_no = m.group(2)

        # Title
        tit_el = li.select_one("p.tit")
        title = tit_el.get_text(strip=True) if tit_el else ""

        # Authors, category, year from division p elements
        authors = ""
        category = ""
        year = ""
        for p in li.select("div.division p"):
            span = p.select_one("span")
            label = span.get_text(strip=True) if span else ""
            full = p.get_text(strip=True)
            value = full[len(label):].strip() if label else full
            if label == "저자":
                authors = value
            elif label == "구분":
                category = value
            elif label == "발행연도":
                year = value

        # PDF path from first .pdf fn_download button (skip .Zip)
        pdf_path = ""
        original_filename = ""
        for btn in li.find_all("button"):
            oc2 = btn.get("onclick", "")
            dm = re.search(r"fn_download\('([^']+)'\)", oc2)
            if dm:
                fp = dm.group(1)
                if fp.lower().endswith(".pdf") and not pdf_path:
                    pdf_path = fp
                    # Decode URL-encoded filename (+ = space in query strings)
                    raw_name = fp.split("/")[-1]
                    original_filename = urllib.parse.unquote(raw_name.replace("+", " "))

        return {
            "schProjectNo": proj_no,
            "schBookResultNo": book_result_no,
            "title": title,
            "authors_list": authors,
            "category": category,
            "year": year,
            "pdf_path": pdf_path,
            "original_filename": original_filename,
        }

    def _parse_list_items_regex(self, raw: str) -> list[dict]:
        """Regex fallback: extract fn_goView pairs from raw HTML."""
        items = []
        seen: set[str] = set()
        for m in re.finditer(r"fn_goView\('([^']+)',\s*'([^']+)'\)", raw):
            proj_no, book_result_no = m.group(1), m.group(2)
            if book_result_no in seen:
                continue
            seen.add(book_result_no)
            items.append({
                "schProjectNo": proj_no,
                "schBookResultNo": book_result_no,
                "title": "",
                "authors_list": "",
                "category": "",
                "year": "",
                "pdf_path": "",
                "original_filename": "",
            })
        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _build_paper(self, item: dict, detail_raw: str, detail_url: str) -> dict[str, Any]:
        """Assemble paper dict from list-item metadata + detail page HTML."""
        soup = self._make_soup(detail_raw, context=f"detail {item['schBookResultNo']}")

        # Title — prefer detail page over list page
        title = item.get("title", "")
        tit_el = soup.select_one(".issue_detail p.title, .inform_box p.title")
        if tit_el:
            t = tit_el.get_text(strip=True)
            if t:
                title = t

        # Parse .inform_box spans for structured metadata
        report_no = ""
        published_year = item.get("year", "")
        authors_full = item.get("authors_list", "")
        category = item.get("category", "")

        for box in soup.select(".inform_box"):
            for p in box.find_all("p"):
                span = p.select_one("span.blue")
                if not span:
                    continue
                label = span.get_text(strip=True)
                value = p.get_text(strip=True)[len(label):].strip()
                if label == "보고서 번호":
                    report_no = value
                elif label == "발행연도":
                    if value:
                        published_year = value
                elif label == "저자":
                    if value:
                        authors_full = value
                elif label == "과제분류":
                    if value:
                        category = value

        # PDF URL — prefer detail page buttons; fall back to list-page pdf_path
        pdf_url = ""
        pdf_path = item.get("pdf_path", "")
        original_filename = item.get("original_filename", "")

        for btn in soup.find_all("button"):
            oc = btn.get("onclick", "")
            dm = re.search(r"fn_download\('([^']+)'\)", oc)
            if dm:
                fp = dm.group(1)
                if fp.lower().endswith(".pdf") and not pdf_url:
                    pdf_url = f"{self._DOWNLOAD_BASE}?filename={fp}"
                    pdf_path = fp
                    raw_name = fp.split("/")[-1]
                    original_filename = urllib.parse.unquote(raw_name.replace("+", " "))

        if not pdf_url and pdf_path:
            pdf_url = f"{self._DOWNLOAD_BASE}?filename={pdf_path}"

        # Abstract from .middle_wrap — the main content paragraph(s)
        abstract = ""
        mw = soup.select_one(".middle_wrap")
        if mw:
            # Replace <br> with space before extracting text
            for br in mw.find_all("br"):
                br.replace_with(" ")
            text = mw.get_text(" ", strip=True)
            abstract = re.sub(r"\s{2,}", " ", text).strip()

        # Fallback: try other content containers if middle_wrap was empty
        if len(abstract) < 100:
            for sel in (".detail_wrap", ".content_box", "main article", ".board_view"):
                node = soup.select_one(sel)
                if node:
                    for br in node.find_all("br"):
                        br.replace_with(" ")
                    text = node.get_text(" ", strip=True)
                    text = re.sub(r"\s{2,}", " ", text).strip()
                    if len(text) >= 100:
                        abstract = text
                        break

        # Authors: detail page gives comma-separated full list → semicolon-separated
        authors_str = authors_full.strip()
        if "," in authors_str:
            authors_str = "; ".join(a.strip() for a in authors_str.split(",") if a.strip())

        # Published date: year only → ISO approximate YYYY-01-01
        published_date = ""
        if published_year and re.match(r"^\d{4}$", published_year.strip()):
            published_date = f"{published_year.strip()}-01-01"

        book_result_no = item["schBookResultNo"]
        proj_no = item["schProjectNo"]

        metadata = {
            "schProjectNo": proj_no,
            "schBookResultNo": book_result_no,
            "report_no": report_no,
            "pdf_path": pdf_path,
            "originalFilename": original_filename,
            "posted_date": published_date,
        }

        return {
            "site_id": self.site_id,
            "external_id": book_result_no,
            "post_number": book_result_no,
            "title": title or f"GRI 보고서 {book_result_no}",
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": authors_str or None,
            "publisher": self._PUBLISHER,
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": pdf_url or None,
            "keywords": None,
            "category": category or None,
            "doi": None,
            "original_filename": original_filename or None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _budget_exhausted(self, t0: float) -> bool:
        return (time.monotonic() - t0) >= (self._MAX_RUNTIME_S - self._RUNTIME_GRACE_S)
