# -*- coding: utf-8 -*-
"""KRIVET 한국직업능력연구원 학술지 crawler (krivet-re-kr-kor).

List page:   GET https://www.krivet.re.kr/kor/sub.do?menuSn=15&pageIndex=N
Detail page: GET https://www.krivet.re.kr/kor/sub.do?menuSn=15&pstNo=PB...

Abstract built from 내용(KOR) + 내용(ENG) <pre> blocks in the detail page.
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler

_SITE_ID = "krivet-re-kr-kor"
_BASE_URL = "https://www.krivet.re.kr"
_LIST_URL = f"{_BASE_URL}/kor/sub.do"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_CLOCK_LIMIT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


def _curl_get(url: str, params: dict | None = None, retries: int = 3) -> str | None:
    """GET via curl with TLS workaround and exponential backoff (1s, 3s, 9s)."""
    if params:
        url = url + "?" + urlencode(params)

    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", f"Referer: {_BASE_URL}/kor/sub.do?menuSn=15",
        url,
    ]

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
            if attempt < retries - 1:
                wait = 3 ** attempt  # 1s, 3s
                time.sleep(wait)
        except subprocess.TimeoutExpired:
            if attempt < retries - 1:
                time.sleep(3 ** attempt)
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(3 ** attempt)
            else:
                print(f"[{_SITE_ID}] curl error after {retries} attempts: {exc}")
    return None


def _make_soup(html: str):
    """Parse HTML with fallback parser chain; returns BeautifulSoup or None."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str:
    """'2025.12.31' or '20251231' → '2025-12-31'."""
    m = re.match(r"(\d{4})[.\-/](\d{2})[.\-/](\d{2})", raw.strip())
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m2 = re.match(r"(\d{4})(\d{2})(\d{2})", raw.strip())
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return raw.strip()


class KrivetReKrKorCrawler(BaseCrawler):
    """Crawler for KRIVET 한국직업능력연구원 학술지."""

    site_id = _SITE_ID
    site_name = "Custom: krivet-re-kr-kor"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Internal fetchers
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page_index: int) -> str | None:
        return _curl_get(_LIST_URL, {"menuSn": "15", "pageIndex": str(page_index)})

    def _fetch_detail_page(self, pst_no: str) -> str | None:
        return _curl_get(_LIST_URL, {"menuSn": "15", "pstNo": pst_no})

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of item dicts from the list page tbody."""
        items = []
        try:
            soup = _make_soup(html)
            if not soup:
                return items
            tbody = soup.find("tbody")
            if not tbody:
                return items
            for row in tbody.find_all("tr"):
                try:
                    cells = row.find_all("td")
                    if len(cells) < 4:
                        continue
                    anchor = cells[1].find("a")
                    if not anchor:
                        continue
                    # onclick="homeRschPublicationsList.selectDetail('PB0000000788');return false;"
                    m = re.search(r"selectDetail\('([^']+)'\)", anchor.get("onclick", ""))
                    if not m:
                        continue
                    pst_no = m.group(1)
                    title = anchor.get_text(strip=True)

                    # English title (pub_sub_tit) and publication (pub_sub_tit2)
                    eng_title = ""
                    publication = ""
                    for p in cells[1].find_all("p"):
                        cls = " ".join(p.get("class", []))
                        if "pub_sub_tit2" in cls:
                            publication = p.get_text(strip=True)
                        elif "pub_sub_tit" in cls:
                            eng_title = p.get_text(strip=True)

                    # Authors — strip button popup elements
                    auth_cell = cells[2]
                    for btn in auth_cell.find_all("button"):
                        btn.decompose()
                    authors_raw = auth_cell.get_text(strip=True)

                    date_text = cells[3].get_text(strip=True)

                    # File info from icon_file button title attribute
                    file_name = ""
                    if len(cells) > 4:
                        file_btn = cells[4].find("button", class_="icon_file")
                        if file_btn:
                            file_name = file_btn.get("title", "")

                    # Attachment number from fileDown call in list row
                    atch_no = ""
                    for btn_any in cells[1].find_all("button") + (
                        [cells[4]] if len(cells) > 4 else []
                    ):
                        onclick_str = btn_any.get("onclick", "") if hasattr(btn_any, "get") else ""
                        am = re.search(
                            r"fileDown\('[^']+',\s*'([^']+)',\s*'(\d+)'",
                            onclick_str,
                        )
                        if am:
                            atch_no = am.group(2)
                            break

                    items.append({
                        "pst_no": pst_no,
                        "title": title,
                        "eng_title": eng_title,
                        "publication": publication,
                        "authors_raw": authors_raw,
                        "date_text": date_text,
                        "file_name": file_name,
                        "atch_no": atch_no,
                    })
                except Exception:
                    continue
        except Exception:
            pass
        return items

    def _parse_detail_page(self, html: str) -> dict | None:
        """Extract full record from the detail page; returns None on hard failure."""
        try:
            soup = _make_soup(html)
            if not soup:
                return None

            # Title
            title_tag = soup.find(class_="pub_tit")
            title = title_tag.get_text(strip=True) if title_tag else ""

            # English title (h5 with pub_sub_tit — inside the view header)
            eng_h5 = soup.find("h5", class_="pub_sub_tit")
            eng_title = eng_h5.get_text(strip=True) if eng_h5 else ""

            # DL key→value metadata: 저자 / 분류정보 / 발행기관 / 발행일 / 등록일
            authors: list[str] = []
            category = ""
            publisher = ""
            published_date = ""
            reg_date = ""

            for dl in soup.find_all("dl", class_="sub_dl"):
                dts = dl.find_all("dt")
                dds = dl.find_all("dd")
                for dt, dd in zip(dts, dds):
                    key = dt.get_text(strip=True)
                    if key in ("저자", "저자명"):
                        for span in dd.find_all("span"):
                            for btn in span.find_all("button"):
                                btn.decompose()
                            name = span.get_text(strip=True)
                            if name:
                                authors.append(name)
                    elif key == "분류정보":
                        category = dd.get_text(strip=True)
                    elif key == "발행기관":
                        publisher = dd.get_text(strip=True)
                    elif key == "발행일":
                        published_date = _parse_date(dd.get_text(strip=True))
                    elif key == "등록일":
                        reg_date = _parse_date(dd.get_text(strip=True))

            # Keywords (주제어 section → rel_box)
            keywords: list[str] = []
            for h5_kw in soup.find_all("h5", class_="sub_tit4"):
                if "주제어" in h5_kw.get_text():
                    rb = h5_kw.find_next("div", class_="rel_box")
                    if rb:
                        keywords = [k.strip() for k in rb.get_text(strip=True).split(",") if k.strip()]
                    break

            # Korean abstract (tl2) + English abstract (tl4)
            kor_abs = ""
            tl2 = soup.find("div", id="tl2")
            if tl2:
                pre = tl2.find("pre")
                if pre:
                    kor_abs = pre.get_text(strip=True)

            eng_abs = ""
            tl4 = soup.find("div", id="tl4")
            if tl4:
                pre = tl4.find("pre")
                if pre:
                    eng_abs = pre.get_text(strip=True)

            abstract = "\n\n".join(p for p in [kor_abs, eng_abs] if p)

            # Attachment number from btn_down button onclick
            atch_no = ""
            atch_pst_no = ""
            btn_down = soup.find("button", class_="btn_down")
            if btn_down:
                am = re.search(
                    r"fileDown\('([^']+)','(\d+)'", btn_down.get("onclick", "")
                )
                if am:
                    atch_pst_no = am.group(1)
                    atch_no = am.group(2)

            return {
                "title": title,
                "eng_title": eng_title,
                "authors": authors,
                "category": category,
                "publisher": publisher,
                "published_date": published_date,
                "reg_date": reg_date,
                "keywords": keywords,
                "abstract": abstract,
                "atch_no": atch_no,
                "atch_pst_no": atch_pst_no,
            }
        except Exception as exc:
            print(f"[{_SITE_ID}] detail parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl KRIVET 학술대회논문 with full pagination.

        Stops when:
          - saved >= limit
          - page returns 0 new pst_no values (empty or loop detected)
          - safety cap of 200 pages
          - wall-clock budget (25 min) exceeded
        """
        saved = 0
        page = 1
        seen_pst: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            # Limit check
            if limit is not None and saved >= limit:
                break

            # Safety cap
            if page > _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            # Wall-clock budget
            if time.time() - start_time > _WALL_CLOCK_LIMIT:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded. Stopping.")
                break

            # Progress log
            if page == 1 or page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # Fetch list page
            time.sleep(self._delay)
            try:
                raw = self._fetch_list_page(page)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] list page {page} fetch error: {exc}. Stopping.")
                break

            if not raw:
                print(f"[{_SITE_ID}] Empty list response at page {page}. Stopping.")
                break

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{_SITE_ID}] No items at page {page}. Done.")
                break

            # URL deduplication / loop guard
            new_items = []
            for item in items:
                if item["pst_no"] not in seen_pst:
                    seen_pst.add(item["pst_no"])
                    new_items.append(item)

            if not new_items:
                print(f"[{_SITE_ID}] Page {page}: all items already seen (pagination loop). Stopping.")
                break

            # Process each new item
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                pst_no = item["pst_no"]

                try:
                    time.sleep(self._delay)
                    detail_html = self._fetch_detail_page(pst_no)
                    if not detail_html:
                        print(f"[{_SITE_ID}] item {pst_no}: empty detail response. Skipping.")
                        continue

                    detail = self._parse_detail_page(detail_html)
                    if not detail:
                        print(f"[{_SITE_ID}] item {pst_no}: parse failed. Skipping.")
                        continue

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] item {pst_no}: abstract too short "
                            f"({len(abstract)} chars). Skipping."
                        )
                        continue

                    title = detail.get("title") or item.get("title") or ""
                    if not title:
                        print(f"[{_SITE_ID}] item {pst_no}: no title. Skipping.")
                        continue

                    authors = detail.get("authors") or []
                    if not authors:
                        raw_auth = re.sub(r"\s*외\s*$", "", item.get("authors_raw", "")).strip()
                        authors = [raw_auth] if raw_auth else []

                    published_date = detail.get("published_date") or _parse_date(item.get("date_text", ""))

                    # PDF download URL (POST endpoint — stored as reference)
                    atch_no = detail.get("atch_no") or item.get("atch_no") or ""
                    file_pst = detail.get("atch_pst_no") or pst_no
                    pdf_url = (
                        f"{_BASE_URL}/kor/com/fileDown.do"
                        f"?fileDownDiv=NOTICOMM&pstgClsfCd=NOTISTDY"
                        f"&pstNo={file_pst}&pstgAtchNo={atch_no}"
                        if atch_no else ""
                    )

                    detail_url = f"{_BASE_URL}/kor/sub.do?menuSn=15&pstNo={pst_no}"

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": pst_no,
                        "title": title,
                        "authors": json.dumps(authors, ensure_ascii=False),
                        "abstract": abstract,
                        "category": detail.get("category") or "",
                        "keywords": json.dumps(detail.get("keywords") or [], ensure_ascii=False),
                        "published_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": detail.get("publisher") or "",
                        "metadata": json.dumps({
                            "eng_title": detail.get("eng_title") or item.get("eng_title") or "",
                            "publication": item.get("publication") or "",
                            "reg_date": detail.get("reg_date") or "",
                            "file_name": item.get("file_name") or "",
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {pst_no} failed: {exc}; continuing")
                    continue

            page += 1

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
