# -*- coding: utf-8 -*-
"""Crawler for mof.go.kr document list (보도자료 — 해양수산부).

List:   https://www.mof.go.kr/doc/ko/selectDocList.do?paginationInfo.currentPageNo=N&menuSeq=971&bbsSeq=10
Detail: https://www.mof.go.kr/doc/ko/selectDoc.do?docSeq=DOCSEQ&menuSeq=971&bbsSeq=10
PDF:    https://www.mof.go.kr/jfile/readDownloadFile.do?fileType=MOF_ARTICLE&fileTypeSeq=DOCSEQ&fileNum=1
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class MofGoKrDocCrawler(BaseCrawler):
    site_id = "mof-go-kr-doc"
    site_name = "Custom: mof-go-kr-doc"
    base_url = "https://www.mof.go.kr"

    _LIST_URL = "https://www.mof.go.kr/doc/ko/selectDocList.do"
    _DETAIL_URL = "https://www.mof.go.kr/doc/ko/selectDoc.do"
    _MENU_SEQ = "971"
    _BBS_SEQ = "10"

    MAX_PAGES = 200
    _MAX_RUNTIME_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
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

            list_url = (
                f"{self._LIST_URL}?paginationInfo.currentPageNo={page}"
                f"&menuSeq={self._MENU_SEQ}&bbsSeq={self._BBS_SEQ}"
            )
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] Empty response for list page {page}; stopping")
                break

            doc_seqs = self._parse_list_seqs(raw)
            if not doc_seqs:
                print(f"[{self.site_id}] No items on list page {page}; stopping")
                break

            new_count = 0
            for doc_seq in doc_seqs:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                detail_url = (
                    f"{self._DETAIL_URL}?docSeq={doc_seq}"
                    f"&menuSeq={self._MENU_SEQ}&bbsSeq={self._BBS_SEQ}"
                )
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_count += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail docSeq={doc_seq}",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(doc_seq, detail_raw, detail_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] docSeq={doc_seq} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item docSeq={doc_seq} failed: {exc}")
                    continue

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: 0 new items; stopping")
                break

            page += 1
        else:
            print(f"[{self.site_id}] Safety cap reached at {self.MAX_PAGES} pages")

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
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_seqs(self, raw: str) -> list[str]:
        """Extract docSeq values from fn_selectDoc('XXXX') calls in list HTML."""
        seqs: list[str] = []
        seen: set[str] = set()
        for m in re.finditer(r"fn_selectDoc\(['\"]?(\d+)['\"]?\)", raw):
            seq = m.group(1)
            if seq not in seen:
                seen.add(seq)
                seqs.append(seq)
        return seqs

    def _make_soup(self, raw: str, *, context: str = "html") -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return BeautifulSoup("", "html.parser")

    def _parse_info_cell(self, soup: BeautifulSoup, label: str) -> str:
        """Extract value text from info list cell matching a label (부서, 등록일, etc.)."""
        for li in soup.select(".bod-info-box .list li"):
            tit = li.select_one(".tit-cell p")
            txt = li.select_one(".txt-cell p")
            if tit and txt and tit.get_text(strip=True) == label:
                return txt.get_text(strip=True)
        return ""

    def _parse_date(self, raw_date: str) -> str | None:
        """Convert '2026.05.29.' → '2026-05-29'. Returns None if unparseable."""
        clean = raw_date.strip().rstrip(".")
        m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", clean)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return clean or None

    # ------------------------------------------------------------------
    # Paper assembly
    # ------------------------------------------------------------------

    def _build_paper(self, doc_seq: str, detail_raw: str, url: str) -> dict[str, Any]:
        soup = self._make_soup(detail_raw, context=f"docSeq={doc_seq}")

        # Title
        tit_el = soup.select_one(".bo-tit")
        title = tit_el.get_text(strip=True) if tit_el else f"MOF Doc {doc_seq}"

        # Info cells
        department = self._parse_info_cell(soup, "부서")
        contact = self._parse_info_cell(soup, "담당자")
        raw_date = self._parse_info_cell(soup, "등록일")
        published_date = self._parse_date(raw_date) if raw_date else None

        # Abstract — full text of the detail view body
        abstract = ""
        view_el = soup.select_one(".bod-detail-view")
        if view_el:
            abstract = re.sub(r"\s+", " ", view_el.get_text(" ", strip=True)).strip()

        # PDF — prefer first PDF attachment; fallback to first any attachment
        pdf_url = None
        original_filename = None
        for a in soup.select(".attach-list a.down-i-btn"):
            href = a.get("href", "")
            if not href or "readDownloadFile" not in href:
                continue
            cls = " ".join(a.get("class", []))
            # Filename: text content excluding .blind spans
            fname_parts: list[str] = []
            for node in a.children:
                if hasattr(node, "get") and "blind" in (node.get("class") or []):
                    continue
                txt = node if isinstance(node, str) else node.get_text()
                cleaned = txt.strip()
                if cleaned:
                    fname_parts.append(cleaned)
            fname = " ".join(fname_parts).strip() or None

            full_url = f"{self.base_url}{href}" if href.startswith("/") else href
            if "pdf" in cls:
                pdf_url = full_url
                original_filename = fname
                break
            if pdf_url is None:
                pdf_url = full_url
                original_filename = fname

        metadata: dict[str, Any] = {
            "docSeq": doc_seq,
            "bbsSeq": self._BBS_SEQ,
            "menuSeq": self._MENU_SEQ,
            "posted_date": raw_date or None,
            "department_raw": department or None,
            "contact_person": contact or None,
        }

        return {
            "site_id": self.site_id,
            "external_id": doc_seq,
            "post_number": doc_seq,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": contact or None,
            "publisher": "해양수산부",
            "department": department or None,
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": None,
            "category": "보도자료",
            "doi": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _budget_exhausted(self, t0: float) -> bool:
        return (time.monotonic() - t0) >= (self._MAX_RUNTIME_S - self._RUNTIME_GRACE_S)
