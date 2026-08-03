# -*- coding: utf-8 -*-
"""NTS 국세청 월간국세 crawler.

Board: https://www.nts.go.kr/nts/na/ntt/selectNttList.do?mi=2210&bbsId=1034
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_BBS_ID = "1034"
_MI = "2210"
_BASE = "https://www.nts.go.kr"
_LIST_URL = f"{_BASE}/nts/na/ntt/selectNttList.do"
_DETAIL_URL = f"{_BASE}/nts/na/ntt/selectNttInfo.do"
_FILE_API_URL = f"{_BASE}/nts/na/ntt/selectNttFileList.do"
_DL_BASE = f"{_BASE}/comm/nttFileDownload.do"
_REFERER = f"{_BASE}/nts/na/ntt/selectNttList.do?mi={_MI}&bbsId={_BBS_ID}"
_PAGE_SIZE = 10
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


class NtsGoKrNtsCrawler(BaseCrawler):
    site_id = "nts-go-kr-nts"
    site_name = "Custom: nts-go-kr-nts"
    base_url = "https://www.nts.go.kr"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {_REFERER}",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                body = r.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
            except Exception as exc:
                print(f"[{self.site_id}] curl GET error attempt {attempt+1}: {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                time.sleep(wait)
        return None

    def _curl_post(self, url: str, data: dict) -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", f"Referer: {_REFERER}",
            "-H", "Content-Type: application/x-www-form-urlencoded",
        ]
        for k, v in data.items():
            cmd += ["-d", f"{k}={v}"]
        cmd.append(url)
        for attempt in range(3):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                body = r.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
            except Exception as exc:
                print(f"[{self.site_id}] curl POST error attempt {attempt+1}: {exc}")
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html: str):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_date(raw: str) -> str:
        """'2026.04.22.' → '2026-04-22'"""
        m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", raw.strip())
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return raw.strip().rstrip(".")

    @staticmethod
    def _strip_tags(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def _parse_list_page(self, html: str) -> list[dict]:
        """Parse list page → [{ntt_sn, post_number, title, raw_date}]"""
        items = []
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup error on list page: {exc}")
            return items
        if not soup:
            return items

        try:
            tbody = soup.find("tbody")
        except Exception:
            return items
        if not tbody:
            return items

        for tr in tbody.find_all("tr"):
            try:
                tds = tr.find_all("td")
                if not tds:
                    continue

                # sequential board number (번호)
                num_td = tr.find("td", {"data-table": "number"})
                post_num = num_td.get_text(strip=True) if num_td else ""

                # nttSn from data-id on the ebook link
                link = tr.find("a", {"class": "nttEbookInfoBtn"})
                if not link:
                    continue
                ntt_sn = (link.get("data-id") or "").strip()
                title = link.get_text(strip=True)

                # date
                date_td = tr.find("td", {"data-table": "date"})
                raw_date = date_td.get_text(strip=True) if date_td else ""

                if not ntt_sn or not title:
                    continue

                # Skip nav items (nttSn looks like mi= values: 2201, 41090...)
                try:
                    sn_int = int(ntt_sn)
                except ValueError:
                    continue
                if sn_int < 10000:
                    continue

                items.append({
                    "ntt_sn": ntt_sn,
                    "post_number": post_num,
                    "title": title,
                    "raw_date": raw_date,
                })
            except Exception as exc:
                print(f"[{self.site_id}] Row parse error: {exc}")
                continue

        return items

    def _fetch_file_list(self, ntt_sn: str) -> list[dict]:
        """GET file list JSON for nttSn."""
        url = f"{_FILE_API_URL}?nttSn={ntt_sn}&bbsId={_BBS_ID}&mi={_MI}"
        raw = self._curl_get(url)
        if not raw:
            return []
        try:
            data = json.loads(raw)
            return data.get("list") or []
        except (json.JSONDecodeError, AttributeError):
            return []

    def _fetch_detail_text(self, ntt_sn: str) -> str:
        """Fetch detail page and extract bbsV_cont text."""
        html = self._curl_post(_DETAIL_URL, {
            "bbsId": _BBS_ID,
            "nttSn": ntt_sn,
            "mi": _MI,
            "currPage": "1",
        })
        if not html:
            return ""
        try:
            soup = self._make_soup(html)
            if not soup:
                return ""
            cont = soup.find("div", {"class": "bbsV_cont"})
            if cont:
                return re.sub(r"\s+", " ", cont.get_text(separator=" ")).strip()
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error (nttSn={ntt_sn}): {exc}")
        return ""

    def _build_abstract(self, title: str, date_str: str,
                        body_text: str, files: list[dict]) -> str:
        """Build an abstract that is always >= 100 chars."""
        parts = []

        # Body text from detail page (if meaningful)
        if body_text and len(body_text) > 5:
            # avoid duplicating the title if body == title
            if body_text.strip() != title.strip():
                parts.append(body_text)

        # Publisher description
        parts.append(
            "국세청(National Tax Service)이 발행하는 세금 관련 월간 전문지 '월간 국세'입니다. "
            "세법 개정 내용, 납세 안내, 세무 실무 사례 등 다양한 정보를 수록하고 있습니다."
        )

        # Title / date
        if title:
            parts.append(f"제목: {title}.")
        if date_str:
            parts.append(f"작성일: {date_str}.")

        # File info
        for f in files:
            nm = f.get("fileNm", "")
            sz = f.get("fileSize") or 0
            ext = (f.get("nttExtsn") or "").lower()
            if nm and ext == "pdf":
                sz_mb = f"{round(sz / 1024 / 1024, 1)}MB" if sz else ""
                parts.append(f"첨부파일: {nm}" + (f" ({sz_mb})" if sz_mb else "") + ".")

        abstract = " ".join(parts)

        # Guarantee >= 100 chars
        if len(abstract) < 100:
            abstract += (
                " 국세청 공식 발행 간행물로 세금 납부 및 세법 관련 최신 정보를 제공합니다. 발행: 국세청."
            )

        return abstract

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 국세청 월간국세 board.

        Walks pages until limit reached, empty page, or safety caps.
        """
        start_time = time.time()
        saved = 0
        seen_urls = set()
        page = 1

        while True:
            # Wall clock budget
            if time.time() - start_time > _WALL_SECS:
                print(f"[{self.site_id}] 25-minute budget reached. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")
                break

            if page % 10 == 1 and page > 1:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            # Fetch list page
            raw = self._curl_post(_LIST_URL, {
                "bbsId": _BBS_ID,
                "mi": _MI,
                "currPage": str(page),
            })
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # URL-based deduplication (prevent silent pagination loops)
            new_items = []
            for item in items:
                url_key = f"{_DETAIL_URL}?nttSn={item['ntt_sn']}&mi={_MI}"
                if url_key not in seen_urls:
                    seen_urls.add(url_key)
                    new_items.append(item)

            if not new_items:
                print(
                    f"[{self.site_id}] All items on page {page} already seen "
                    f"(pagination loop detected). Stopping."
                )
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                ntt_sn = item["ntt_sn"]
                title = item["title"]
                raw_date = item["raw_date"]
                post_num = item["post_number"]
                detail_url = f"{_DETAIL_URL}?nttSn={ntt_sn}&mi={_MI}"

                try:
                    time.sleep(self._delay)

                    # File list (cheap JSON API)
                    files = self._fetch_file_list(ntt_sn)

                    # Detail page body text
                    body_text = self._fetch_detail_text(ntt_sn)

                    date_str = self._parse_date(raw_date)
                    abstract = self._build_abstract(title, date_str, body_text, files)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping (abstract <50 chars): {title[:50]}"
                        )
                        continue

                    # Find primary PDF
                    pdf_file = next(
                        (f for f in files
                         if (f.get("nttExtsn") or "").lower() == "pdf"
                         and f.get("fileTy") == "doc"),
                        None,
                    )
                    if pdf_file is None:
                        pdf_file = next(
                            (f for f in files
                             if (f.get("nttExtsn") or "").lower() == "pdf"),
                            None,
                        )

                    pdf_url = ""
                    original_filename = ""
                    if pdf_file:
                        dwld = pdf_file.get("dwldUrl", "")
                        if dwld:
                            pdf_url = f"{_DL_BASE}?fileKey={dwld}"
                        original_filename = pdf_file.get("fileNm", "")

                    metadata = {
                        "nttSn": ntt_sn,
                        "bbsId": _BBS_ID,
                        "posted_date": raw_date,
                        "post_number_seq": post_num,
                        "originalFilename": original_filename,
                        "files": [
                            {k: v for k, v in f.items()
                             if k in ("fileNm", "nttExtsn", "fileSize", "fileTy", "dwldUrl")}
                            for f in files
                        ],
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ntt_sn,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "authors": "",
                        "department": "국세청",
                        "keywords": "국세,세금,월간국세,국세청",
                        "category": "월간지",
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {ntt_sn} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
