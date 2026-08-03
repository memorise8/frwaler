# -*- coding: utf-8 -*-
"""Crawler for kinfa.or.kr press releases (보도자료).

List:   POST https://www.kinfa.or.kr/notificationPromotion/news.do  (currentPageNo=N)
Detail: GET  https://www.kinfa.or.kr/notificationPromotion/newsDetail.do?seq=N
File:   https://www.kinfa.or.kr/FileDown.do?seqFile=N&fileSize=N

Content is often image-based; abstract is built from board-detail header when
the content div is empty (title + date + attachment filenames = reliably >= 100 chars).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from typing import Any

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class KinfaOrKrNotificationPromotioCrawler(BaseCrawler):
    site_id = "kinfa-or-kr-notificationpromotio"
    site_name = "Custom: kinfa-or-kr-notificationpromotio"
    base_url = "https://www.kinfa.or.kr"

    _LIST_URL = "https://www.kinfa.or.kr/notificationPromotion/news.do"
    _DETAIL_URL = "https://www.kinfa.or.kr/notificationPromotion/newsDetail.do"
    _FILE_BASE = "https://www.kinfa.or.kr/FileDown.do"
    _DEPARTMENT = "서민금융진흥원"

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
        last_page = None

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

            list_raw = self._curl_post(
                self._LIST_URL,
                data={"currentPageNo": str(page)},
                context=f"list page {page}",
            )
            if not list_raw:
                print(f"[{self.site_id}] Empty response for list page {page}; stopping")
                break

            if last_page is None:
                last_page = self._detect_last_page(list_raw)
                if last_page:
                    print(f"[{self.site_id}] Detected {last_page} total pages")

            seqs = self._parse_list_seqs(list_raw)
            if not seqs:
                print(f"[{self.site_id}] No items on list page {page}; stopping")
                break

            new_count = 0
            for seq in seqs:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                detail_url = f"{self._DETAIL_URL}?seq={seq}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_count += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail seq={seq}",
                        referer=self._LIST_URL,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(seq, detail_raw, detail_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] seq={seq} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_label}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item seq={seq} failed: {exc}")
                    continue

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: 0 new items; stopping")
                break

            if last_page and page >= last_page:
                print(f"[{self.site_id}] Reached last page ({last_page}); stopping")
                break

            page += 1
        else:
            print(f"[{self.site_id}] Safety cap reached at {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_post(
        self,
        url: str,
        data: dict[str, str],
        *,
        context: str = "request",
        timeout: int = 45,
    ) -> str | None:
        data_str = "&".join(f"{k}={v}" for k, v in data.items())
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"Referer: {self._LIST_URL}",
            "--data", data_str,
            url,
        ]
        return self._run_curl(cmd, context=context, timeout=timeout)

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
        return self._run_curl(cmd, context=context, timeout=timeout)

    def _run_curl(
        self,
        cmd: list[str],
        *,
        context: str,
        timeout: int,
    ) -> str | None:
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

    def _make_soup(self, raw: str, *, context: str = "html") -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return BeautifulSoup("", "html.parser")

    def _detect_last_page(self, raw: str) -> int:
        """Return the highest page number found in fn_link_page() calls (the '끝' button)."""
        nums = re.findall(r"fn_link_page\((\d+)\)", raw)
        if not nums:
            return 0
        return max(int(x) for x in nums)

    def _parse_list_seqs(self, raw: str) -> list[int]:
        """Extract seqBoardContents values from list page items in order."""
        soup = self._make_soup(raw, context="list page")
        items = soup.select("li.item.type01")
        seqs: list[int] = []
        seen: set[int] = set()
        for item in items:
            # BeautifulSoup lowercases all attribute names
            seq_str = item.get("data-seqboardcontents", "")
            if not seq_str:
                continue
            try:
                seq = int(seq_str)
                if seq not in seen:
                    seen.add(seq)
                    seqs.append(seq)
            except ValueError:
                pass

        # Fallback: regex scan for the attribute in raw HTML
        if not seqs:
            for m in re.finditer(r'data-seqBoardContents="(\d+)"', raw, re.IGNORECASE):
                seq = int(m.group(1))
                if seq not in seen:
                    seen.add(seq)
                    seqs.append(seq)

        return seqs

    def _build_paper(self, seq: int, detail_raw: str, url: str) -> dict[str, Any]:
        soup = self._make_soup(detail_raw, context=f"detail seq={seq}")

        # --- Title ---
        title = ""
        tit_el = soup.select_one(".board-detail-header .tit")
        if tit_el:
            title = re.sub(r"\s+", " ", tit_el.get_text(" ", strip=True)).strip()
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                raw_title = title_tag.get_text(strip=True)
                # Page titles like "보도자료 상세보기 > 보도자료 > 새소식 > 알림·홍보|서민금융진흥원"
                title = raw_title.split(">")[0].strip() or raw_title
        if not title:
            title = f"서민금융진흥원 보도자료 {seq}"

        # --- Published date (YYYY-MM-DD) ---
        published_date = ""
        for li in soup.select(".info-list li"):
            txt = li.get_text(strip=True)
            if re.match(r"\d{4}-\d{2}-\d{2}", txt):
                published_date = txt
                break

        # --- Abstract ---
        # Primary: text from the content div (usually image-only → empty)
        abstract = ""
        con_el = soup.select_one(".board-detail-con")
        if con_el:
            abstract = re.sub(r"\s+", " ", con_el.get_text(" ", strip=True)).strip()

        # Fallback: full board-detail area minus the prev/next navigation.
        # Gives: title + date + "첨부파일" + attachment filenames = reliably >= 100 chars.
        if len(abstract) < 50:
            det_el = soup.select_one(".board-detail")
            if det_el:
                for nav in det_el.select(".board-detail-prev-next, .board-detail-footer"):
                    nav.decompose()
                fallback = re.sub(r"\s+", " ", det_el.get_text(" ", strip=True)).strip()
                if len(fallback) > len(abstract):
                    abstract = fallback

        # --- PDF/HWP attachment URL ---
        # JS: fn_fileDown('seqFile','fileSize') → window.open('/FileDown.do?seqFile=N&fileSize=N')
        pdf_url = ""
        pdf_m = re.search(r"fn_fileDown\('(\d+)'\s*,\s*'(\d+)'\)", detail_raw)
        if pdf_m:
            pdf_url = f"{self._FILE_BASE}?seqFile={pdf_m.group(1)}&fileSize={pdf_m.group(2)}"

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
            "site_id": self.site_id,
            "external_id": str(seq),
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "보도자료",
            "keywords": json.dumps(["보도자료", "서민금융진흥원"], ensure_ascii=False),
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": self._DEPARTMENT,
            "metadata": json.dumps({"seq": seq}, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _budget_exhausted(self, t0: float) -> bool:
        return (time.monotonic() - t0) >= (self._MAX_RUNTIME_S - self._RUNTIME_GRACE_S)
