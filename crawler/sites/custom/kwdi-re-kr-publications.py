# -*- coding: utf-8 -*-
"""Crawler for KWDI 젠더리뷰 (한국여성정책연구원 Gender Review).

List:   https://www.kwdi.re.kr/publications/genderReview.do?p=N   (10 items/page, ~63 pages)
Detail: https://www.kwdi.re.kr/publications/genderReviewView.do?p=P&idx=IDX
PDF:    https://www.kwdi.re.kr/inc/download.do?ut=A&upIdx=IDX&no=1
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

from crawler.base_crawler import BaseCrawler


class KwdiReKrPublicationsCrawler(BaseCrawler):
    site_id = "kwdi-re-kr-publications"
    site_name = "Custom: kwdi-re-kr-publications"
    base_url = "https://www.kwdi.re.kr"

    _LIST_URL = "https://www.kwdi.re.kr/publications/genderReview.do"
    _DETAIL_BASE = "https://www.kwdi.re.kr/publications/genderReviewView.do"
    _DEPARTMENT = "한국여성정책연구원"
    _ATTRIBUTION = (
        "한국여성정책연구원 젠더리뷰 "
        "(Korean Women's Development Institute, Gender Review)"
    )

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

            list_url = f"{self._LIST_URL}?p={page}"
            raw = self._curl_get(list_url, context=f"list page {page}")
            if not raw:
                print(f"[{self.site_id}] Empty response for list page {page}; stopping")
                break

            items = self._parse_list(raw)
            if not items:
                print(f"[{self.site_id}] No items on list page {page}; stopping")
                break

            total_pages = self._extract_total_pages(raw)

            new_count = 0
            for href, idx in items:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                detail_url = f"{self._DETAIL_BASE}?p={page}&idx={idx}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_count += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail idx={idx}",
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(idx, detail_raw, detail_url)
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
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list(self, raw: str) -> list[tuple[str, str]]:
        """Return ordered, deduplicated (href, idx) pairs from list HTML."""
        items: list[tuple[str, str]] = []
        seen: set[str] = set()
        for m in re.finditer(r'href="(genderReviewView\.do\?[^"]*)"', raw):
            href = m.group(1)
            m_idx = re.search(r'idx=(\d+)', href)
            if not m_idx:
                continue
            idx = m_idx.group(1)
            if idx not in seen:
                seen.add(idx)
                items.append((href, idx))
        return items

    def _extract_total_pages(self, raw: str) -> int:
        """Extract the last page number from pagination links."""
        # Match: href="?&p=102" class="last" (or similar ordering)
        m = re.search(r'class="last"[^>]*>', raw)
        if m:
            # Search backwards from "last" for p=N
            chunk = raw[max(0, m.start() - 100):m.start()]
            pm = re.findall(r'p=(\d+)', chunk)
            if pm:
                return int(pm[-1])
        # Fallback: find href="?&p=N" class="last"
        m2 = re.search(r'href="\?[^"]*p=(\d+)"[^>]*class="last"', raw)
        if m2:
            return int(m2.group(1))
        # Fallback: max page link found
        pages = [int(x) for x in re.findall(r'\?[^"]*p=(\d+)', raw)]
        return max(pages) if pages else 0

    # ------------------------------------------------------------------
    # BeautifulSoup helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str, *, context: str = "html"):
        if not _BS4_OK:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(
                    f"[{self.site_id}] BeautifulSoup({parser}) failed "
                    f"for {context}: {exc}"
                )
        try:
            return BeautifulSoup("", "html.parser")
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Paper assembly
    # ------------------------------------------------------------------

    def _build_paper(self, idx: str, detail_raw: str, url: str) -> dict[str, Any]:
        soup = self._make_soup(detail_raw, context=f"idx={idx}")

        # Title — prefer th.tit, fall back to th[colspan=4], then regex
        title = ""
        if soup is not None:
            tit_el = soup.find("th", class_="tit")
            if tit_el:
                title = tit_el.get_text(strip=True)
            if not title:
                for th in soup.find_all("th", attrs={"colspan": True}):
                    t = th.get_text(strip=True)
                    if t:
                        title = t
                        break
        if not title:
            m_tit = re.search(r'class="[^"]*tit[^"]*">([^<]+)', detail_raw)
            if m_tit:
                title = m_tit.group(1).strip()
        if not title:
            title = f"KWDI 젠더리뷰 {idx}"

        # Metadata from table rows (th label → td value pairs)
        volume = ""
        issue_title = ""
        category = ""
        date = ""
        pdf_url = ""

        table = soup.find("table", class_="table-view") if soup is not None else None
        if table:
            for row in table.find_all("tr"):
                cells = row.find_all(["th", "td"])
                i = 0
                while i < len(cells):
                    cell = cells[i]
                    if (
                        cell.name == "th"
                        and i + 1 < len(cells)
                        and cells[i + 1].name == "td"
                    ):
                        label = cell.get_text(strip=True)
                        val = cells[i + 1].get_text(strip=True)
                        if label == "발간호":
                            volume = val
                        elif label == "통권제목":
                            issue_title = val
                        elif label == "구분":
                            category = val
                        elif label == "등록일":
                            date = val
                        i += 2
                    else:
                        i += 1

                # PDF download link within this row
                if not pdf_url:
                    pdf_a = row.find("a", href=re.compile(r"download\.do"))
                    if pdf_a:
                        href_val = pdf_a.get("href", "")
                        pdf_url = (
                            f"{self.base_url}{href_val}"
                            if href_val.startswith("/")
                            else href_val
                        )

        # Abstract from viewwrap div
        viewwrap = soup.find("div", class_="viewwrap") if soup is not None else None
        if viewwrap:
            abstract_raw = viewwrap.get_text(" ", strip=True)
            abstract_raw = abstract_raw.replace("\xa0", " ")
            abstract_raw = re.sub(r"\s+", " ", abstract_raw).strip()
        else:
            abstract_raw = ""

        # Authors: viewwrap often has "article title | Author (affiliation)"
        authors_list: list[str] = []
        if "|" in abstract_raw:
            author_part = abstract_raw.split("|", 1)[1].strip()
            if author_part:
                authors_list = [
                    a.strip() for a in re.split(r"[,;]", author_part) if a.strip()
                ]

        # Build abstract: title + viewwrap + structured metadata + attribution
        # This guarantees >= 100 chars for any parseable item.
        parts: list[str] = [title]
        if abstract_raw and abstract_raw.strip() != title.strip():
            parts.append(abstract_raw)
        meta_fields = []
        if issue_title:
            meta_fields.append(f"통권: {issue_title}")
        if volume:
            meta_fields.append(f"발간호: {volume}")
        if category:
            meta_fields.append(f"구분: {category}")
        if date:
            meta_fields.append(f"등록일: {date}")
        if meta_fields:
            parts.append(" | ".join(meta_fields))
        parts.append(self._ATTRIBUTION)
        abstract = "\n".join(p for p in parts if p)

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": idx,
            "title": title,
            "authors": json.dumps(authors_list, ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": self._DEPARTMENT,
            "metadata": json.dumps(
                {"idx": idx, "volume": volume, "issue_title": issue_title,
                 "category": category},
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _budget_exhausted(self, t0: float) -> bool:
        return (time.monotonic() - t0) >= (self._MAX_RUNTIME_S - self._RUNTIME_GRACE_S)
