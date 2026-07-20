# -*- coding: utf-8 -*-
"""Crawler for KIHASA press releases (보도자료).

List:   https://www.kihasa.re.kr/news/press/list?page=N
Detail: https://www.kihasa.re.kr/news/press/view?seq=XXXXX
PDF:    https://www.kihasa.re.kr/api/kihasa/file/download?seq=XXXXX

The site is Nuxt.js SSR — all data is embedded in window.__NUXT__ on each
page as a minified IIFE. Items on the list page carry seq/title/regDate;
full HTML body ("contents") is only on the detail page.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class KihasaReKrNewsCrawler(BaseCrawler):
    site_id = "kihasa-re-kr-news"
    site_name = "Custom: kihasa-re-kr-news"
    base_url = "https://www.kihasa.re.kr"

    _LIST_URL = "https://www.kihasa.re.kr/news/press/list"
    _DETAIL_BASE = "https://www.kihasa.re.kr/news/press/view"
    _PDF_BASE = "https://www.kihasa.re.kr/api/kihasa/file/download"
    _DEPARTMENT = "한국보건사회연구원"

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

            seqs = self._parse_list_seqs(raw)
            if not seqs:
                print(f"[{self.site_id}] No items on list page {page}; stopping")
                break

            total_pages = self._extract_int(raw, "totalPage")

            new_count = 0
            for seq in seqs:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                detail_url = f"{self._DETAIL_BASE}?seq={seq}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_count += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail seq={seq}",
                        referer=list_url,
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
            if total_pages and page >= total_pages:
                print(f"[{self.site_id}] Reached last page ({total_pages}); stopping")
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

    def _nuxt_state(self, raw: str) -> str:
        """Extract the window.__NUXT__ IIFE string (single line in SSR output)."""
        m = re.search(r"window\.__NUXT__\s*=\s*(.+)", raw)
        return m.group(1) if m else ""

    def _extract_int(self, raw: str, key: str) -> int:
        """Extract the first literal integer value for a JS property key."""
        m = re.search(rf"\b{re.escape(key)}:(\d+)", raw)
        return int(m.group(1)) if m else 0

    def _parse_list_seqs(self, raw: str) -> list[int]:
        """Return ordered, deduplicated seq values from the list page."""
        nuxt = self._nuxt_state(raw)
        seqs: list[int] = []
        seen: set[int] = set()

        if nuxt:
            for m in re.finditer(r"\bseq:(\d+)", nuxt):
                s = int(m.group(1))
                if s not in seen:
                    seen.add(s)
                    seqs.append(s)

        # Fallback: scan raw HTML for detail hrefs
        if not seqs:
            for m in re.finditer(r"/news/press/view\?seq=(\d+)", raw):
                s = int(m.group(1))
                if s not in seen:
                    seen.add(s)
                    seqs.append(s)

        return seqs

    def _extract_js_string(self, text: str, key: str) -> str:
        """Forward-scan to decode a JS string property value.

        Handles \\uXXXX unicode escapes and standard backslash sequences.
        Avoids regex backtracking on very large strings (e.g. contents HTML).
        """
        for pat in (f'"{key}":"', f'{key}:"'):
            idx = text.find(pat)
            if idx == -1:
                continue
            i = idx + len(pat)
            buf: list[str] = []
            _simple = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\", "/": "/", "b": "\b", "f": "\f"}
            while i < len(text):
                c = text[i]
                if c == "\\" and i + 1 < len(text):
                    nc = text[i + 1]
                    if nc == "u" and i + 5 < len(text):
                        hex4 = text[i + 2: i + 6]
                        try:
                            buf.append(chr(int(hex4, 16)))
                            i += 6
                            continue
                        except ValueError:
                            pass
                    buf.append(_simple.get(nc, nc))
                    i += 2
                elif c == '"':
                    return "".join(buf)
                else:
                    buf.append(c)
                    i += 1
        return ""

    def _decode_js_str(self, s: str) -> str:
        """Decode \\uXXXX escapes in an already-extracted JS string fragment."""
        return re.sub(
            r"\\u([0-9a-fA-F]{4})",
            lambda m: chr(int(m.group(1), 16)),
            s,
        )

    def _make_soup(self, raw: str, *, context: str = "html") -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return BeautifulSoup("", "html.parser")

    # ------------------------------------------------------------------
    # Paper assembly
    # ------------------------------------------------------------------

    def _build_paper(self, seq: int, detail_raw: str, url: str) -> dict[str, Any]:
        nuxt = self._nuxt_state(detail_raw)

        # --- Title ---
        title = ""
        # Primary: content:{seq:NNN,title:"..."}
        tm = re.search(r'content:\{seq:\d+,title:"((?:[^"\\]|\\.)*?)"', nuxt)
        if tm:
            title = self._decode_js_str(tm.group(1))
        # Fallback: any title: field
        if not title:
            title = self._extract_js_string(nuxt, "title")
        if not title:
            title = f"KIHASA 보도자료 {seq}"

        # --- Published date (regDate is Unix ms timestamp) ---
        published_date = ""
        dm = re.search(r"\bregDate:(\d+)", nuxt)
        if dm:
            published_date = self._ms_to_date(int(dm.group(1)))

        # --- Abstract: decode contents HTML then strip tags ---
        abstract = ""
        contents_html = self._extract_js_string(nuxt, "contents")
        if contents_html:
            try:
                soup = self._make_soup(contents_html, context=f"seq={seq} contents")
                text = soup.get_text(" ", strip=True)
                abstract = re.sub(r"\s+", " ", text).strip()
            except Exception as exc:
                print(f"[{self.site_id}] seq={seq} contents parse failed: {exc}")

        # Fallback: scan raw detail HTML for main content
        if len(abstract) < 50 and detail_raw:
            try:
                dsoup = self._make_soup(detail_raw, context=f"seq={seq} detail-html")
                for sel in (".se-contents", ".view-content", ".board-view", "main", "article"):
                    node = dsoup.select_one(sel)
                    if node:
                        text = re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()
                        if len(text) >= 50:
                            abstract = text
                            break
            except Exception as exc:
                print(f"[{self.site_id}] seq={seq} html fallback parse failed: {exc}")

        # --- PDF URL from files array ---
        pdf_url = ""
        fm = re.search(r"\bfileSeq:(\d+)", nuxt)
        if fm:
            pdf_url = f"{self._PDF_BASE}?seq={fm.group(1)}"

        return {
            "id": str(uuid.uuid5(uuid.NAMESPACE_URL, url)),
            "site_id": self.site_id,
            "external_id": str(seq),
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "보도자료",
            "keywords": json.dumps(["보도자료", "보건사회연구원"], ensure_ascii=False),
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

    @staticmethod
    def _ms_to_date(ts_ms: int) -> str:
        return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
