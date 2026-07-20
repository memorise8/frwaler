# -*- coding: utf-8 -*-
"""중소벤처기업부 조사보고서 crawler (기관별 조회).

List:   GET  https://www.mss.go.kr/site/smba/foffice/ex/statDB/stReportRoList.do
             ?gb=1&nodeId=&roCode=&pageIndex=N&pageSize=10&recordCountPerPage=10&searchGubun=roCode
Detail: POST https://www.mss.go.kr/site/smba/foffice/ex/statDB/StReportContentDetailView.do
             ?gb=1&nodeId=&rcCode=&roCode=   body: reSeq=ID
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# BeautifulSoup helper — fallback chain: html5lib → lxml → html.parser
# ---------------------------------------------------------------------------

def _make_soup(raw: str):
    """Parse HTML; tries html5lib first, falls back gracefully. Returns None on total failure."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _strip_html(html_str: str) -> str:
    """Strip HTML tags and normalize whitespace."""
    text = re.sub(r"<[^>]+>", " ", html_str or "")
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#?\w+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class MssGoKrSiteCrawler(BaseCrawler):
    """중소벤처기업부 조사보고서 (기관별 조회) crawler."""

    site_id   = "mss-go-kr-site"
    site_name = "Custom: mss-go-kr-site"
    base_url  = "https://www.mss.go.kr"

    _LIST_URL   = "https://www.mss.go.kr/site/smba/foffice/ex/statDB/stReportRoList.do"
    _DETAIL_URL = ("https://www.mss.go.kr/site/smba/foffice/ex/statDB/"
                   "StReportContentDetailView.do?gb=1&nodeId=&rcCode=&roCode=")
    _PAGE_SIZE  = 10
    _MAX_PAGES  = 200          # safety cap; ~2 000 records total on site
    _MAX_SECS   = 25 * 60     # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, post_data: dict | None = None, retries: int = 3) -> str | None:
        """GET (or POST if post_data given) via curl; returns decoded text or None."""
        cmd = ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30"]
        if post_data:
            cmd += ["-X", "POST", "-d", urllib.parse.urlencode(post_data)]
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")
            if attempt < retries - 1:
                wait = (attempt + 1) ** 2  # 1s, 4s (close to 1s/3s/9s req)
                time.sleep(wait)
        return None

    def _fetch_list(self, page: int) -> str | None:
        params = urllib.parse.urlencode({
            "gb": "1",
            "nodeId": "",
            "roCode": "",
            "pageIndex": str(page),
            "pageSize": str(self._PAGE_SIZE),
            "recordCountPerPage": str(self._PAGE_SIZE),
            "searchGubun": "roCode",
        })
        url = f"{self._LIST_URL}?{params}"
        return self._curl(url)

    def _fetch_detail(self, re_seq: str) -> str | None:
        data = {
            "pageIndex": "1",
            "pageSize": str(self._PAGE_SIZE),
            "recordCountPerPage": str(self._PAGE_SIZE),
            "searchGubun": "roCode",
            "reSeq": str(re_seq),
            "rfSeq": "",
            "sort": "",
        }
        return self._curl(self._DETAIL_URL, post_data=data)

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of {reSeq, title, date, org, file_hrefs} from one list page."""
        items: list[dict] = []
        soup = _make_soup(html)
        if not soup:
            return items

        tbody = soup.find("tbody")
        if not tbody:
            return items

        for tr in tbody.find_all("tr", recursive=False):
            # Skip "no data" rows
            if tr.find(class_="board_null") or "등록된 게시물이 없습니다" in tr.get_text():
                continue

            # reSeq and title — from the desktop subject cell <a>
            subject_td = tr.find("td", class_="subject")
            if not subject_td:
                continue
            link = subject_td.find("a")
            if not link:
                continue
            href = link.get("href", "")
            m = re.search(r"doStReportDetailView\('(\d+)'\)", href)
            if not m:
                continue
            re_seq = m.group(1)
            title = link.get_text(strip=True)

            # Org
            org_td = tr.find("td", class_="writeOrg")
            org = org_td.get_text(strip=True) if org_td else ""

            # Date — first bare <td> that matches YYYY/MM/DD
            date = ""
            for td in tr.find_all("td"):
                txt = td.get_text(strip=True)
                if re.fullmatch(r"\d{4}/\d{2}/\d{2}", txt):
                    date = txt.replace("/", "-")
                    break

            # Attached file hrefs
            file_hrefs = [
                span["data-href"]
                for span in tr.find_all("span", class_="single-file")
                if span.get("data-href")
            ]

            items.append({
                "reSeq": re_seq,
                "title": title,
                "date": date,
                "org": org,
                "file_hrefs": file_hrefs,
            })

        return items

    def _parse_detail_page(self, html: str, list_item: dict) -> dict | None:
        """Parse detail page HTML; return paper dict or None."""
        soup = _make_soup(html)
        if soup:
            return self._parse_detail_soup(soup, html, list_item)
        # Total BS4 failure — fall back to regex
        return self._parse_detail_regex(html, list_item)

    def _parse_detail_soup(self, soup, html: str, item: dict) -> dict | None:
        bv = soup.find(class_="board_view")
        if not bv:
            return self._parse_detail_regex(html, item)

        # Title
        h4 = bv.find("h4")
        title = h4.get_text(strip=True) if h4 else item.get("title", "")

        # Metadata from "web" rows (desktop version — avoids mobile duplicates)
        category = ""
        pub_date = item.get("date", "")
        org = item.get("org", "")
        frequency = ""

        for tr in bv.find_all("tr", class_="web"):
            ths = tr.find_all("th")
            tds = tr.find_all("td")
            for i, th in enumerate(ths):
                key = th.get_text(strip=True)
                val = tds[i].get_text(strip=True) if i < len(tds) else ""
                if key == "분야":
                    category = val
                elif key == "발행일":
                    pub_date = val.replace("/", "-")
                elif key == "작성기관":
                    org = val
                elif key == "조사주기":
                    frequency = val

        # Abstract from contents_box
        cb = bv.find("td", class_="contents_box")
        if cb:
            abstract = cb.get_text(separator=" ", strip=True)
        else:
            # Fallback: largest <td> by text length
            tds = bv.find_all("td")
            abstract = max(
                (td.get_text(separator=" ", strip=True) for td in tds),
                key=len,
                default="",
            )
        abstract = re.sub(r"\s+", " ", abstract).strip()

        # Attached files (from detail page first, else list item)
        file_hrefs = [
            s["data-href"]
            for s in bv.find_all("span", class_="single-file")
            if s.get("data-href")
        ]
        if not file_hrefs:
            file_hrefs = item.get("file_hrefs", [])

        pdf_url = (self.base_url + file_hrefs[0]) if file_hrefs else ""
        re_seq = item["reSeq"]
        detail_url = (
            f"https://www.mss.go.kr/site/smba/foffice/ex/statDB/"
            f"StReportContentDetailView.do?gb=1&nodeId=&rcCode=&roCode=&reSeq={re_seq}"
        )

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": re_seq,
            "title": title or item.get("title", ""),
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": pub_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": org,
            "metadata": json.dumps({
                "frequency": frequency,
                "fileHrefs": file_hrefs,
            }, ensure_ascii=False),
        }

    def _parse_detail_regex(self, html: str, item: dict) -> dict | None:
        """Pure-regex fallback when BeautifulSoup is unavailable or fails."""
        re_seq = item["reSeq"]

        title = item.get("title", "")
        h4_m = re.search(r"<h4[^>]*>([^<]+)</h4>", html)
        if h4_m:
            title = h4_m.group(1).strip()

        cb_m = re.search(r'class="contents_box"[^>]*>(.*?)</td>', html, re.DOTALL)
        abstract = _strip_html(cb_m.group(1)) if cb_m else ""

        pub_date = item.get("date", "")
        pd_m = re.search(r"발행일[\s\S]{0,200}?(\d{4}/\d{2}/\d{2})", html)
        if pd_m:
            pub_date = pd_m.group(1).replace("/", "-")

        cat_m = re.search(r"분야[\s\S]{0,100}?<td[^>]*>([^<]+)</td>", html)
        category = cat_m.group(1).strip() if cat_m else ""

        file_hrefs = re.findall(r'data-href="([^"]+)"', html) or item.get("file_hrefs", [])
        pdf_url = (self.base_url + file_hrefs[0]) if file_hrefs else ""
        detail_url = (
            f"https://www.mss.go.kr/site/smba/foffice/ex/statDB/"
            f"StReportContentDetailView.do?gb=1&nodeId=&rcCode=&roCode=&reSeq={re_seq}"
        )

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": re_seq,
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": pub_date,
            "url": detail_url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": item.get("org", ""),
            "metadata": json.dumps({"fileHrefs": file_hrefs}, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 중소벤처기업부 조사보고서 (기관별 조회).

        Walks list pages until limit is reached, no new records appear,
        or safety caps (200 pages / 25 min) are hit.
        """
        saved = 0
        seen_ids: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        t0 = time.monotonic()
        page = 1

        while True:
            # --- pre-page guards ---
            elapsed = time.monotonic() - t0
            if elapsed > self._MAX_SECS:
                print(f"[{self.site_id}] Wall-clock budget ({self._MAX_SECS}s) exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            # --- fetch list page ---
            list_html = self._fetch_list(page)
            if not list_html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            list_items = self._parse_list_page(list_html)
            if not list_items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # Pagination-loop guard: if first item on this page is already seen,
            # the paginator silently looped back to page 1.
            if list_items[0]["reSeq"] in seen_ids and page > 1:
                print(f"[{self.site_id}] Pagination loop detected at page {page}. Stopping.")
                break

            any_new = False
            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                re_seq = item["reSeq"]
                if re_seq in seen_ids:
                    continue
                seen_ids.add(re_seq)
                any_new = True

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail(re_seq)
                    if not detail_html:
                        print(f"[{self.site_id}] item reSeq={re_seq} fetch failed: empty response")
                        continue

                    paper = self._parse_detail_page(detail_html, item)
                    if not paper:
                        print(f"[{self.site_id}] item reSeq={re_seq} failed: parse returned None")
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item reSeq={re_seq} skipped: "
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
                    print(f"[{self.site_id}] item reSeq={re_seq} failed: {exc}")
                    continue

            if not any_new:
                print(f"[{self.site_id}] No new items on page {page}. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
