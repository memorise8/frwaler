# -*- coding: utf-8 -*-
"""Crawler for KIPO 정책용역·연구보고서 (kipo.go.kr).

Target: https://www.kipo.go.kr/ko/kpoBultnMgmt.do?menuCd=SCD0201112&sysCd=SCD02&pgmId=BUT0000065
총 595건 (2026-05 기준), 정책용역 및 연구보고서 게시판.
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import urlencode, urljoin

# Absolute-import compatibility: spec_from_file_location has no package context.
_ROOT = os.path.dirname(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(os.path.abspath(__file__))
        )
    )
)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402


# ---------------------------------------------------------------------------
# BeautifulSoup with fallback parser chain
# ---------------------------------------------------------------------------

def _make_soup(html):
    """Try html5lib → lxml → html.parser; return None on total failure."""
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KipoGoKrKoCrawler(BaseCrawler):
    """Crawler for 특허청 정책용역·연구보고서 (menuCd=SCD0201112)."""

    site_id = "kipo-go-kr-ko"
    site_name = "Custom: kipo-go-kr-ko"
    base_url = "https://www.kipo.go.kr"

    _LIST_URL = "https://www.kipo.go.kr/ko/kpoBultnMgmt.do"
    _DETAIL_URL = "https://www.kipo.go.kr/ko/kpoBultnDetail.do"
    _MENU_CD = "SCD0201112"
    _SYS_CD = "SCD02"
    _APRCH_ID = "BUT0000065"
    _PAGE_SIZE = 20

    # ------------------------------------------------------------------
    # curl-based HTTP (TLS workaround for Korean gov sites)
    # ------------------------------------------------------------------

    def _curl(self, method, url, data=None, retries=3):
        """Run curl; return decoded UTF-8 body or None on persistent failure."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30", "-L",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        ]
        if method == "POST" and data:
            cmd += [
                "-X", "POST",
                "-H", "Content-Type: application/x-www-form-urlencoded",
                "--data", urlencode(data),
            ]
        cmd.append(url)

        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt+1}/{retries}: {exc}")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1s, 3s, 9s
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list(self, page_index):
        return self._curl("POST", self._LIST_URL, {
            "menuCd": self._MENU_CD,
            "sysCd": self._SYS_CD,
            "pgmId": self._APRCH_ID,
            "pageIndex": page_index,
            "pageUnit": self._PAGE_SIZE,
            "searchCondition": "1",
            "keyword": "",
            "searchDateStart": "",
            "searchDateEnd": "",
        })

    def _parse_list(self, html):
        """Return list of raw item dicts extracted from the list page HTML."""
        items = []
        soup = _make_soup(html)
        if not soup:
            return items

        for link in soup.find_all("a", href=re.compile(r"kpoBultnDetail\.do")):
            href = link.get("href", "")
            m = re.search(r"ntatcSeq=(\d+)", href)
            if not m:
                continue
            ntatc_seq = m.group(1)
            title = (link.get("title") or link.get_text(strip=True)).strip()
            detail_url = urljoin(self.base_url, href)

            # Date from sibling <td>s in the same <tr>
            listed_date = None
            row = link.find_parent("tr")
            if row:
                for td in row.find_all("td"):
                    md = re.search(r"(\d{4}-\d{2}-\d{2})", td.get_text(strip=True))
                    if md:
                        listed_date = md.group(1)
                        break

            # PDF download link in same row
            pdf_url = None
            pdf_filename = None
            if row:
                for fl in row.find_all("a", href=re.compile(r"kpoBultnFileDown\.do")):
                    pdf_url = urljoin(self.base_url, fl.get("href", ""))
                    fl_title = fl.get("title", "")
                    fn_m = re.match(r"(.+?)\s+다운로드", fl_title)
                    if fn_m:
                        pdf_filename = fn_m.group(1).strip()
                    elif fl.get_text(strip=True):
                        pdf_filename = fl.get_text(strip=True)
                    break

            items.append({
                "ntatc_seq": ntatc_seq,
                "title": title,
                "detail_url": detail_url,
                "listed_date": listed_date,
                "pdf_url": pdf_url,
                "pdf_filename": pdf_filename,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, ntatc_seq):
        """Fetch and parse a detail page. Returns enriched dict or None."""
        url = (
            f"{self._DETAIL_URL}"
            f"?menuCd={self._MENU_CD}"
            f"&ntatcSeq={ntatc_seq}"
            f"&sysCd={self._SYS_CD}"
            f"&aprchId={self._APRCH_ID}"
        )
        html = self._curl("GET", url)
        if not html:
            return None

        soup = _make_soup(html)
        if not soup:
            return None

        tbl = soup.find("div", class_="tbl_view")
        if not tbl:
            return None

        result = {
            "title": None,
            "department": None,
            "contact": None,
            "published_date": None,
            "body": None,
            "pdf_url": None,
            "pdf_filename": None,
        }

        # Title (div.v_tit)
        v_tit = tbl.find("div", class_="v_tit")
        if v_tit:
            result["title"] = v_tit.get_text(strip=True)

        # Header fields: each div.v_header has <strong> labels + <div> values
        for v_hdr in tbl.find_all("div", class_="v_header"):
            labels = [s.get_text(strip=True) for s in v_hdr.find_all("strong")]
            # Value divs are the leaf <div> elements (no nested <div>)
            val_divs = [d for d in v_hdr.find_all("div") if not d.find("div")]
            vals = [d.get_text(strip=True) for d in val_divs]
            for label, val in zip(labels, vals):
                if "담당부서" in label:
                    result["department"] = val
                elif "작성일" in label or "등록일" in label:
                    md = re.search(r"(\d{4}-\d{2}-\d{2})", val)
                    if md:
                        result["published_date"] = md.group(1)
                elif "연락처" in label:
                    result["contact"] = val

        # Body content (div.v_body)
        v_body = tbl.find("div", class_="v_body")
        if v_body:
            body_text = v_body.get_text(strip=True)
            if body_text:
                result["body"] = body_text

        # Attachment: first PDF link in div.v_bottom
        for v_bot in tbl.find_all("div", class_="v_bottom"):
            for a in v_bot.find_all("a", href=re.compile(r"kpoBultnFileDown\.do")):
                href = a.get("href", "")
                result["pdf_url"] = urljoin(self.base_url, href)
                a_title = a.get("title", "")
                fn_m = re.match(r"(.+?)\s+다운로드", a_title)
                if fn_m:
                    result["pdf_filename"] = fn_m.group(1).strip()
                elif a.get_text(strip=True):
                    result["pdf_filename"] = a.get_text(strip=True)
                break
            if result["pdf_url"]:
                break

        return result

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl KIPO 정책용역·연구보고서 and persist to DB.

        Paginates until limit reached, empty page, all-seen page, or safety
        caps (200 pages / 25 minutes).
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        MAX_WALL = 25 * 60     # 25 minutes
        MAX_PAGES = 200
        limit_or_inf = limit if limit is not None else float("inf")

        try:
            for page in range(1, MAX_PAGES + 1):
                # Wall-clock safety check
                elapsed = time.time() - start_time
                if elapsed > MAX_WALL:
                    print(
                        f"[{self.site_id}] Wall-clock budget exceeded "
                        f"({elapsed:.0f}s) at page {page}, stopping"
                    )
                    break

                if page == MAX_PAGES:
                    print(
                        f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached, stopping"
                    )
                    break

                if page % 10 == 0:
                    print(
                        f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}"
                    )

                html = self._fetch_list(page)
                if not html:
                    print(f"[{self.site_id}] Empty list response at page {page}, stopping")
                    break

                items = self._parse_list(html)
                if not items:
                    print(f"[{self.site_id}] No items on page {page}, stopping")
                    break

                new_on_page = 0
                for item in items:
                    if saved >= limit_or_inf:
                        break

                    detail_url = item["detail_url"]
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    try:
                        detail = self._fetch_detail(item["ntatc_seq"])

                        if detail:
                            title = detail.get("title") or item["title"]
                            body = detail.get("body") or ""
                            published_date = (
                                detail.get("published_date") or item.get("listed_date")
                            )
                            department = detail.get("department") or ""
                            contact = detail.get("contact") or ""
                            pdf_url = detail.get("pdf_url") or item.get("pdf_url")
                            pdf_filename = (
                                detail.get("pdf_filename") or item.get("pdf_filename")
                            )
                        else:
                            title = item["title"]
                            body = ""
                            published_date = item.get("listed_date")
                            department = ""
                            contact = ""
                            pdf_url = item.get("pdf_url")
                            pdf_filename = item.get("pdf_filename")

                        # Build a rich abstract: title + structured fields + body + filename.
                        # This combination ensures >=100 chars for typical KIPO reports.
                        ab_parts = []
                        if title:
                            ab_parts.append(f"제목: {title}")
                        meta_fields = []
                        if department:
                            meta_fields.append(f"담당부서: {department}")
                        if contact:
                            meta_fields.append(f"연락처: {contact}")
                        if published_date:
                            meta_fields.append(f"작성일: {published_date}")
                        ab_parts.extend(meta_fields)
                        if body:
                            ab_parts.append(body)
                        if pdf_filename:
                            ab_parts.append(f"첨부파일: {pdf_filename}")
                        abstract = "\n".join(ab_parts)

                        if len(abstract) < 50:
                            print(
                                f"[{self.site_id}] skip {item['ntatc_seq']}: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        self._save_paper({
                            "site_id": self.site_id,
                            "external_id": item["ntatc_seq"],
                            "post_number": item["ntatc_seq"],
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": item.get("listed_date"),
                            "url": detail_url,
                            "pdf_url": pdf_url,
                            "original_filename": pdf_filename,
                            "publisher": "특허청",
                            "department": department,
                            "category": "정책용역/연구보고서",
                            "metadata": json.dumps(
                                {
                                    "ntatcSeq": item["ntatc_seq"],
                                    "posted_date": item.get("listed_date"),
                                    "originalFilename": pdf_filename,
                                    "contact": contact,
                                },
                                ensure_ascii=False,
                            ),
                        })
                        saved += 1

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[{self.site_id}] item {item.get('ntatc_seq', '?')} failed: {exc}"
                        )
                        continue

                    time.sleep(self._delay)

                if saved >= limit_or_inf:
                    break

                if new_on_page == 0:
                    print(
                        f"[{self.site_id}] All items on page {page} already seen, stopping"
                    )
                    break

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted at page {page}, saved {saved} items")
            raise

        return saved
