# -*- coding: utf-8 -*-
"""Crawler for KDCA 보도자료 (질병관리청).

List:   https://www.kdca.go.kr/bbs/kdca/41/artclList.do?layout=...&page=N&pageSize=10
Detail: https://www.kdca.go.kr/bbs/kdca/41/{artclSeq}/artclView.do?layout=unknown

Row numbers (td-num) are used as post_number for incremental crawling.
Article seq numbers are used as external_id.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from typing import Any

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class KdcaGoKrKdcaCrawler(BaseCrawler):
    site_id = "kdca-go-kr-kdca"
    site_name = "Custom: kdca-go-kr-kdca"
    base_url = "https://www.kdca.go.kr"

    _LIST_URL = "https://www.kdca.go.kr/bbs/kdca/41/artclList.do"
    _LIST_LAYOUT = "6b6463614040323834374040666e637431"
    _DETAIL_TMPL = "https://www.kdca.go.kr/bbs/kdca/41/{seq}/artclView.do?layout=unknown"
    _PUBLISHER = "질병관리청"

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

            list_url = (
                f"{self._LIST_URL}?layout={self._LIST_LAYOUT}"
                f"&page={page}&pageSize=10"
            )
            list_raw = self._curl_get(list_url, context=f"list page {page}",
                                      referer=f"{self.base_url}/kdca/2847/subview.do")
            if not list_raw:
                print(f"[{self.site_id}] Empty list response on page {page}; stopping")
                break

            items = self._parse_list_items(list_raw)
            if not items:
                print(f"[{self.site_id}] No items on page {page}; stopping")
                break

            new_count = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                if self._budget_exhausted(t0):
                    print(f"[{self.site_id}] Runtime budget exhausted; stopping cleanly")
                    return saved

                detail_url = self._DETAIL_TMPL.format(seq=item["artcl_seq"])
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_count += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        detail_url,
                        context=f"detail seq={item['artcl_seq']}",
                        referer=list_url,
                    )
                    if not detail_raw:
                        raise RuntimeError("empty detail response")

                    paper = self._build_paper(item, detail_raw, detail_url)
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] seq={item['artcl_seq']} skipped: "
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
                    print(f"[{self.site_id}] item seq={item['artcl_seq']} failed: {exc}")
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
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
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

    def _parse_list_items(self, raw: str) -> list[dict[str, str]]:
        """Return ordered list of {artcl_seq, post_number, listed_date} dicts."""
        soup = self._make_soup(raw, context="list page")
        tbody = soup.find("tbody")
        if not tbody:
            return []

        items: list[dict[str, str]] = []
        for tr in tbody.find_all("tr"):
            num_td = tr.find("td", class_="td-num")
            # Extract artcl_seq from jf_viewArtcl JS call (3rd param)
            artcl_m = re.search(
                r"jf_viewArtcl\s*\([^,]+,\s*['\"][^'\"]+['\"],\s*['\"](\d+)['\"]",
                str(tr),
            )
            if not num_td or not artcl_m:
                continue

            post_number = num_td.get_text(strip=True)
            artcl_seq = artcl_m.group(1)

            # Listed date from td with date class or first date-shaped text
            date_td = tr.find("td", class_=re.compile(r"td-date|date"))
            listed_date = ""
            if date_td:
                listed_date = self._parse_date(date_td.get_text(strip=True))
            if not listed_date:
                date_m = re.search(r"(\d{4}\.\d{1,2}\.\d{1,2})", str(tr))
                if date_m:
                    listed_date = self._parse_date(date_m.group(1))

            items.append({
                "artcl_seq": artcl_seq,
                "post_number": post_number,
                "listed_date": listed_date,
            })
        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _build_paper(
        self,
        item: dict[str, str],
        detail_raw: str,
        detail_url: str,
    ) -> dict[str, Any]:
        soup = self._make_soup(detail_raw, context=f"detail seq={item['artcl_seq']}")

        # Title — hidden input is most reliable; fall back to strong inside .title
        title = ""
        title_inp = soup.find("input", {"id": "artclViewTitle"})
        if title_inp:
            title = (title_inp.get("value") or "").strip()
        if not title:
            view_div = soup.find("div", class_=re.compile(r"view"))
            if view_div:
                strong = view_div.find("strong")
                if strong:
                    title = strong.get_text(strip=True)
        if not title:
            title = f"KDCA 보도자료 {item['artcl_seq']}"

        # Published date (작성일)
        published_date = ""
        detail_ul = soup.find("ul", class_="detail")
        if detail_ul:
            for li in detail_ul.find_all("li"):
                span = li.find("span")
                if span and "작성일" in span.get_text():
                    raw_date = li.get_text(strip=True).replace("작성일", "").strip()
                    published_date = self._parse_date(raw_date)
                    break

        # Department (담당부서)
        department = ""
        if detail_ul:
            for li in detail_ul.find_all("li"):
                span = li.find("span")
                if span and "담당부서" in span.get_text():
                    department = li.get_text(strip=True).replace("담당부서", "").strip()
                    break

        # Abstract — from div.txt, with image alt fallback
        abstract = self._extract_abstract(soup, item["artcl_seq"])

        # Attachments — prefer PDF, then anything
        pdf_url, original_filename = self._extract_attachment(soup)

        # Category from breadcrumb / menu
        category = "보도자료"

        meta: dict[str, Any] = {
            "artcl_seq": item["artcl_seq"],
            "post_number": item["post_number"],
            "board_id": "41",
        }

        return {
            "site_id": self.site_id,
            "external_id": item["artcl_seq"],
            "post_number": item["post_number"],
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": item.get("listed_date") or published_date,
            "authors": "",
            "publisher": self._PUBLISHER,
            "department": department,
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": category,
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    def _extract_abstract(self, soup: BeautifulSoup, seq: str) -> str:
        txt_div = soup.find("div", class_="txt")
        if not txt_div:
            # Broader fallback
            for cls in ("artcl-cnt", "view-body", "board-txt", "ck-content"):
                txt_div = soup.find("div", class_=cls)
                if txt_div:
                    break

        if txt_div:
            # Primary: get text content (strips tags, decodes HTML entities)
            text = txt_div.get_text(" ", strip=True)
            text = re.sub(r"\s+", " ", text).strip()
            if len(text) >= 100:
                return text

            # Fallback: collect img alt attributes (card-news style articles)
            alts = []
            for img in txt_div.find_all("img"):
                alt = (img.get("alt") or "").strip()
                if alt:
                    alts.append(alt)
            alt_text = " ".join(alts)
            alt_text = re.sub(r"\s+", " ", alt_text).strip()
            if len(alt_text) >= len(text):
                return alt_text
            return text

        return ""

    def _extract_attachment(self, soup: BeautifulSoup) -> tuple[str, str]:
        """Return (pdf_url, original_filename) from the attachment section."""
        attach_div = soup.find("div", class_="attachment")
        if not attach_div:
            return "", ""

        pdf_url = ""
        pdf_filename = ""
        hwp_url = ""
        hwp_filename = ""

        for li in attach_div.find_all("li"):
            dl_a = li.find("a", class_="attch-down")
            if not dl_a:
                continue
            href = dl_a.get("href", "")
            if not href:
                continue
            # Get filename from text node before the <div>
            fname = ""
            for child in li.children:
                text = getattr(child, "string", None) or (
                    str(child) if not hasattr(child, "find") else ""
                )
                if isinstance(text, str):
                    fname_cand = text.strip()
                    if fname_cand and not fname_cand.startswith("<"):
                        fname = fname_cand
                        break

            full_url = (
                href if href.startswith("http") else f"{self.base_url}{href}"
            )
            lower = (fname or href).lower()
            if ".pdf" in lower and not pdf_url:
                pdf_url = full_url
                pdf_filename = fname
            elif (".hwp" in lower or ".hwpx" in lower) and not hwp_url:
                hwp_url = full_url
                hwp_filename = fname

        if pdf_url:
            return pdf_url, pdf_filename
        if hwp_url:
            return hwp_url, hwp_filename
        # Any first attachment
        for li in attach_div.find_all("li"):
            dl_a = li.find("a", class_="attch-down")
            if dl_a and dl_a.get("href"):
                href = dl_a["href"]
                return (
                    href if href.startswith("http") else f"{self.base_url}{href}",
                    "",
                )
        return "", ""

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    def _make_soup(self, raw: str, *, context: str = "html") -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return BeautifulSoup("", "html.parser")

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Normalise various Korean date strings to YYYY-MM-DD."""
        if not raw:
            return ""
        m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", raw)
        if m:
            y, mo, d = m.group(1), m.group(2).zfill(2), m.group(3).zfill(2)
            return f"{y}-{mo}-{d}"
        return raw.strip()

    def _budget_exhausted(self, t0: float) -> bool:
        return (time.monotonic() - t0) >= (self._MAX_RUNTIME_S - self._RUNTIME_GRACE_S)
