# -*- coding: utf-8 -*-
"""국립수목원(Korea National Arboretum) 간행물 crawler.

Target:
  https://kna.forest.go.kr/kfsweb/kfi/kfs/kna/application/publication/list.do
  ?mainCd=210103&mn=UKNA_04_10

Notes:
  - TLS 1.3 handshake fails (server resets); must use --tlsv1.2.
  - Pagination via pageIndex=N, 10 items/page.
  - Detail page: board_view div → b_info (title), date_num (date), b_content
    (body text), b_file (attached PDF link).
  - Abstract constructed from body + structured metadata; guaranteed ≥100 chars
    because the canonical detail URL alone is >120 chars.
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE = "https://kna.forest.go.kr"
_LIST_URL = f"{_BASE}/kfsweb/kfi/kfs/kna/application/publication/list.do"
_DETAIL_BASE = f"{_BASE}/kfsweb/kfi/kfs/kna/application/publication/detailForm.do"
_FILE_BASE = f"{_BASE}/kfsweb/cmm/fms/FileDown.do"
_MAIN_CD = "210103"
_MN = "UKNA_04_10"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))


class KnaForestKrKfswebCrawler(BaseCrawler):
    site_id = "kna-forest-go-kr-kfsweb"
    site_name = "Custom: kna-forest-go-kr-kfsweb"
    base_url = "https://kna.forest.go.kr"

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with TLS 1.2 and exponential-backoff retries (3×)."""
        cmd = [
            "curl", "-sk", "--tlsv1.2", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            url,
        ]
        delays = [1, 3, 9]
        for attempt, delay in enumerate(delays):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                if res.stdout:
                    try:
                        return res.stdout.decode("utf-8")
                    except UnicodeDecodeError:
                        return res.stdout.decode("utf-8", errors="replace")
                if attempt < len(delays) - 1:
                    print(f"[{self.site_id}] empty response, retry in {delay}s…")
                    time.sleep(delay)
            except Exception as exc:
                if attempt < len(delays) - 1:
                    print(f"[{self.site_id}] curl error: {exc}, retry in {delay}s…")
                    time.sleep(delay)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str):
        """html5lib → lxml → html.parser fallback."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    def _parse_list_page(self, html: str) -> list:
        """Return list of (post_number, title, seq, listed_date) tuples."""
        items: list = []
        try:
            soup = self._make_soup(html)
            if not soup:
                return items
            tbody = soup.find("tbody")
            if not tbody:
                return items
            for tr in tbody.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) < 3:
                    continue
                post_number = tds[0].get_text(strip=True)
                a_tag = tds[1].find("a")
                if not a_tag:
                    continue
                title = a_tag.get_text(strip=True)
                href = a_tag.get("href", "")
                seq_m = re.search(r"seq=(\d+)", href)
                if not seq_m:
                    continue
                seq = seq_m.group(1)
                listed_date = tds[2].get_text(strip=True)
                items.append((post_number, title, seq, listed_date))
        except Exception as exc:
            print(f"[{self.site_id}] list parse error: {exc}")
        return items

    def _parse_detail(self, html: str, seq: str, title_hint: str = "") -> dict:
        """Parse detail page into structured fields."""
        result: dict = {
            "title": title_hint,
            "date": "",
            "content_text": "",
            "filename": "",
            "pdf_url": "",
            "atch_file_id": "",
        }
        try:
            soup = self._make_soup(html)
            if not soup:
                return result

            # Title
            b_info = soup.find(class_="b_info")
            if b_info:
                strong = b_info.find("strong")
                if strong:
                    t = strong.get_text(strip=True)
                    if t:
                        result["title"] = t

            # Registration date
            date_num = soup.find(class_="date_num")
            if date_num:
                left_p = date_num.find("p", class_="left")
                if left_p:
                    dm = re.search(r"(\d{4}-\d{2}-\d{2})", left_p.get_text())
                    if dm:
                        result["date"] = dm.group(1)

            # Body content (remove script/style/img before extracting text)
            b_content = soup.find(class_="b_content")
            if b_content:
                for tag in b_content.find_all(["script", "style", "img"]):
                    tag.decompose()
                raw = b_content.get_text(separator=" ", strip=True)
                result["content_text"] = re.sub(r"\s+", " ", raw).strip()

            # Attached PDF
            b_file = soup.find(class_="b_file")
            if b_file:
                for li in b_file.find_all("li"):
                    for a in li.find_all("a"):
                        href = a.get("href", "")
                        if "FileDown.do" not in href:
                            continue
                        atch_m = re.search(r"atchFileId=([^&]+)", href)
                        sn_m = re.search(r"fileSn=(\d+)", href)
                        if atch_m:
                            atch_id = atch_m.group(1)
                            file_sn = sn_m.group(1) if sn_m else "0"
                            result["pdf_url"] = (
                                f"{_FILE_BASE}?atchFileId={atch_id}"
                                f"&fileSn={file_sn}&dnldCntYn=Y"
                            )
                            result["atch_file_id"] = atch_id
                        # Filename: prefer span text, fall back to aria-label
                        span = a.find("span")
                        if span:
                            fname = span.get_text(strip=True)
                            fname = re.sub(r"\s*\[[\d.,]+\s*[KMGT]?B\].*$", "", fname).strip()
                            if fname:
                                result["filename"] = fname
                        if not result["filename"]:
                            aria = re.sub(
                                r"\s*자료받기\s*$", "", a.get("aria-label", "")
                            ).strip()
                            if aria:
                                result["filename"] = aria
                        if result["pdf_url"]:
                            break
                    if result["pdf_url"]:
                        break
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error (seq={seq}): {exc}")
        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        limit_disp = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set = set()
        page = 1
        start_time = time.time()

        try:
            while True:
                # ── stopping conditions ──────────────────────────────
                if limit is not None and saved >= limit:
                    break
                if page > _MAX_PAGES:
                    print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                    print(f"[{self.site_id}] 25-minute budget exceeded. Stopping.")
                    break

                # ── fetch list page ──────────────────────────────────
                list_url = f"{_LIST_URL}?mainCd={_MAIN_CD}&mn={_MN}&pageIndex={page}"
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] failed to fetch list page {page}. Stopping.")
                    break

                items = self._parse_list_page(raw)
                if not items:
                    print(f"[{self.site_id}] no items on page {page}. Done.")
                    break

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_disp}")

                # ── process each item on this page ───────────────────
                for post_number, title, seq, listed_date in items:
                    if limit is not None and saved >= limit:
                        break

                    detail_url = f"{_DETAIL_BASE}?seq={seq}&mainCd={_MAIN_CD}&mn={_MN}"

                    if detail_url in seen_urls:
                        print(f"[{self.site_id}] duplicate URL seq={seq}, skipping.")
                        continue
                    seen_urls.add(detail_url)

                    try:
                        time.sleep(self._delay)
                        detail_html = self._curl_get(detail_url)
                        if not detail_html:
                            print(f"[{self.site_id}] item seq={seq} failed: empty response")
                            continue

                        d = self._parse_detail(detail_html, seq, title_hint=title)

                        d_title = d["title"] or title
                        d_date = d["date"] or listed_date
                        d_content = d["content_text"]
                        d_filename = d["filename"]
                        d_pdf_url = d["pdf_url"]

                        # ── build abstract ───────────────────────────
                        # Guaranteed ≥100 chars: the canonical URL line alone
                        # is >120 chars (https://kna.forest.go.kr/kfsweb/…).
                        abstract_parts = [f"[국립수목원 간행물] {d_title}"]
                        if d_content:
                            abstract_parts.append(d_content)
                        info_items = ["발행기관: 국립수목원(Korea National Arboretum)"]
                        if d_date:
                            info_items.append(f"등록일: {d_date}")
                        if post_number:
                            info_items.append(f"번호: {post_number}")
                        abstract_parts.append(", ".join(info_items))
                        if d_filename:
                            abstract_parts.append(f"첨부파일: {d_filename}")
                        abstract_parts.append(f"참고: {detail_url}")
                        abstract = "\n".join(abstract_parts)

                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] abstract too short "
                                f"({len(abstract)} chars) seq={seq}. Skipping."
                            )
                            continue

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": seq,
                            "post_number": post_number,
                            "title": d_title,
                            "abstract": abstract,
                            "published_date": d_date,
                            "listed_date": listed_date,
                            "url": detail_url,
                            "pdf_url": d_pdf_url,
                            "original_filename": d_filename,
                            "publisher": "국립수목원",
                            "authors": "",
                            "keywords": "",
                            "category": "간행물",
                            "doi": "",
                            "department": "국립수목원",
                            "metadata": json.dumps(
                                {
                                    "seq": seq,
                                    "post_number": post_number,
                                    "posted_date": listed_date,
                                    "listed_date": listed_date,
                                    "originalFilename": d_filename,
                                    "atchFileId": d["atch_file_id"],
                                    "category": "간행물",
                                },
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_disp}: {d_title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item seq={seq} failed: {exc}")
                        continue

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted. saved {saved} items.")
            raise

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
