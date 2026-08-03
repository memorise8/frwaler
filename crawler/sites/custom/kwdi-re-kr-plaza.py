# -*- coding: utf-8 -*-
"""Crawler for KWDI 보도자료 (한국여성정책연구원 – 열린광장 > 보도자료).

List:   https://www.kwdi.re.kr/plaza/bodo.do?p=N   (10 items/page)
Detail: https://www.kwdi.re.kr/plaza/bodoView.do?p=P&idx=IDX
PDF:    https://www.kwdi.re.kr/inc/download.do?ut=A&upIdx=IDX&no=N
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


def _make_soup(html: str):
    """Return BeautifulSoup with parser fallback chain."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class KwdiReKrPlazaCrawler(BaseCrawler):
    site_id = "kwdi-re-kr-plaza"
    site_name = "Custom: kwdi-re-kr-plaza"
    base_url = "https://www.kwdi.re.kr"

    _LIST_URL = "https://www.kwdi.re.kr/plaza/bodo.do"
    _DETAIL_BASE = "https://www.kwdi.re.kr/plaza/bodoView.do"
    _DEPARTMENT = "한국여성정책연구원"

    MAX_PAGES = 200
    _MAX_RUNTIME_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_idxs: set[str] = set()
        limit_label = str(limit) if limit is not None else "inf"
        t0 = time.monotonic()

        if limit is not None and limit <= 0:
            print(f"[{self.site_id}] Done. Total saved: 0")
            return 0

        while page <= self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break
            if time.monotonic() - t0 > self._MAX_RUNTIME_S:
                print(f"[{self.site_id}] 25-minute budget reached, exiting cleanly.")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            list_url = f"{self._LIST_URL}?p={page}"
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] Empty response for list page {page}; stopping")
                break

            items = self._parse_list(raw)
            if not items:
                print(f"[{self.site_id}] No items on page {page}; stopping")
                break

            new_count = 0
            for idx, title_hint, date_hint, pdf_hint in items:
                if limit is not None and saved >= limit:
                    break
                if time.monotonic() - t0 > self._MAX_RUNTIME_S:
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                if idx in seen_idxs:
                    continue
                seen_idxs.add(idx)
                new_count += 1

                detail_url = f"{self._DETAIL_BASE}?p={page}&idx={idx}"

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail idx={idx}",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(
                        idx, detail_raw, detail_url,
                        title_hint, date_hint, pdf_hint, page,
                    )
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] idx={idx} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_label}: "
                        f"{paper['title'][:60]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item idx={idx} failed: {exc}")
                    continue

            if new_count == 0:
                print(f"[{self.site_id}] page {page}: 0 new items (dedup stop)")
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
    # Parsing – list page
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html or "")
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z#\d]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    def _parse_list(self, raw: str) -> list[tuple[str, str, str, str]]:
        """Return list of (idx, title_hint, date_hint, pdf_hint_href)."""
        items: list[tuple[str, str, str, str]] = []
        seen: set[str] = set()
        try:
            soup = _make_soup(raw)
            if not soup:
                return items
            for row in soup.select("table.table-list tr"):
                link = row.select_one("td.subject a")
                if not link:
                    continue
                href = link.get("href", "")
                m = re.search(r"idx=(\d+)", href)
                if not m:
                    continue
                idx = m.group(1)
                if idx in seen:
                    continue
                seen.add(idx)

                title_hint = link.get_text(strip=True)

                date_hint = ""
                for td in row.find_all("td"):
                    txt = td.get_text(strip=True)
                    if re.match(r"\d{4}-\d{2}-\d{2}", txt):
                        date_hint = txt
                        break

                # Prefer PDF preview link from list row as a hint
                pdf_hint = ""
                file_link = row.select_one("td a.file")
                if file_link:
                    fhref = file_link.get("href", "")
                    ftxt = file_link.get_text(strip=True).lower()
                    if ".pdf" in ftxt:
                        pdf_hint = fhref
                    elif not pdf_hint and fhref:
                        pdf_hint = fhref

                items.append((idx, title_hint, date_hint, pdf_hint))
        except Exception as exc:
            print(f"[{self.site_id}] list-parse error: {exc}")
        return items

    # ------------------------------------------------------------------
    # Parsing – detail page
    # ------------------------------------------------------------------

    def _build_paper(
        self,
        idx: str,
        raw: str,
        detail_url: str,
        title_hint: str,
        date_hint: str,
        pdf_hint: str,
        page: int,
    ) -> dict:
        title = title_hint
        date = date_hint
        abstract = ""
        pdf_url = ""

        try:
            soup = _make_soup(raw)
            if soup:
                # Title from th.tit
                tit = soup.select_one("th.tit")
                if tit:
                    title = tit.get_text(strip=True) or title

                # Date from 배포일 row
                for th in soup.find_all("th"):
                    if "배포일" in th.get_text():
                        td = th.find_next_sibling("td")
                        if td:
                            date = td.get_text(strip=True) or date
                        break

                # PDF download links – prefer .pdf, fallback to .hwp/.hwpx
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if "/inc/download.do" not in href:
                        continue
                    name = a.get_text(strip=True).lower()
                    if ".pdf" in name:
                        pdf_url = self.base_url + href
                        break
                    if not pdf_url and (".hwp" in name or ".hwpx" in name):
                        pdf_url = self.base_url + href

                # Abstract from viewwrap div
                viewwrap = soup.select_one("div.viewwrap")
                if viewwrap:
                    abstract = self._strip_tags(str(viewwrap))

        except Exception as exc:
            print(f"[{self.site_id}] detail-parse error (idx={idx}): {exc}")

        # Fallback pdf_url from list hint
        if not pdf_url and pdf_hint:
            pdf_url = (self.base_url + pdf_hint) if pdf_hint.startswith("/") else pdf_hint

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": idx,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "보도자료",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": self._DEPARTMENT,
            "metadata": json.dumps(
                {"source": "kwdi-plaza-bodo", "page": page},
                ensure_ascii=False,
            ),
        }
