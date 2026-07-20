# -*- coding: utf-8 -*-
"""한국농촌경제연구원 (KREI) 보도자료 crawler.

Starting URL: https://www.krei.re.kr/krei/page/24
Pagination:   ?pageIndex=N  (10 items/page, ~2111 total, ~212 pages)
Detail URL:   https://www.krei.re.kr/krei/page/24?cmd=view&pst=NNNNNN&pageIndex=1
PDF download: /krei/board/atchDown.do?no=NNNNNN
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    """Parse HTML with fallback parser chain; returns None on total failure."""
    if _BS is None:
        return None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception:
            continue
    return None


def _strip_tags(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"&nbsp;", " ", s)
    s = re.sub(r"&#39;", "'", s)
    s = re.sub(r"&#\d+;", "", s)
    s = re.sub(r"&[a-zA-Z]+;", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _parse_date(raw: str) -> str:
    """Convert '2026.05.14' / '2026-05-14' / '20260514' → 'YYYY-MM-DD'."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4})[./\-](\d{1,2})(?:[./\-](\d{1,2}))?", raw)
    if m:
        y, mo = m.group(1), m.group(2).zfill(2)
        d = (m.group(3) or "01").zfill(2)
        return f"{y}-{mo}-{d}"
    m2 = re.match(r"(\d{4})(\d{2})(\d{2})", raw)
    if m2:
        return f"{m2.group(1)}-{m2.group(2)}-{m2.group(3)}"
    return raw


class KreiReKrKreiCrawler(BaseCrawler):
    """Crawler for 한국농촌경제연구원 보도자료."""

    site_id   = "krei-re-kr-krei"
    site_name = "Custom: krei-re-kr-krei"
    base_url  = "https://www.krei.re.kr"

    _LIST_URL   = "https://www.krei.re.kr/krei/page/24"
    _MAX_PAGES  = 200
    _PAGE_SIZE  = 10
    _RATE_SLEEP = 1.0
    _MAX_WALL   = 25 * 60  # 25-minute budget

    # ------------------------------------------------------------------
    # Low-level HTTP via curl (TLS workaround)
    # ------------------------------------------------------------------

    def _curl(self, url: str, params: dict | None = None) -> str | None:
        """GET via curl with SSL workaround; retries up to 3×."""
        full_url = url
        if params:
            full_url = f"{url}?{urllib.parse.urlencode(params)}"

        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            "-H", "Referer: https://www.krei.re.kr/krei/page/24",
            full_url,
        ]

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                body = result.stdout.decode("utf-8", errors="replace")
                if body.strip():
                    return body
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[{self.site_id}] empty response (attempt {attempt+1}/3), "
                          f"retry in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}, "
                          f"retry in {wait}s")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # List page parser
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> list[dict]:
        """Return list of item dicts from one list page; empty = no more pages."""
        raw = self._curl(self._LIST_URL, {"pageIndex": page})
        if not raw:
            return []
        soup = _make_soup(raw)
        if soup is None:
            return []

        items: list[dict] = []

        # Items are inside <section class="noti_sect"> → <div class="item ...">
        noti = soup.find("section", class_="noti_sect")
        if noti is None:
            # Fallback: search whole page
            noti = soup

        for div in noti.find_all("div", class_=lambda c: c and "item" in c.split()):
            a_tag = div.find("a", href=re.compile(r"pst=\d+"))
            if not a_tag:
                continue

            href = a_tag.get("href", "")
            m_pst = re.search(r"pst=(\d+)", href)
            if not m_pst:
                continue
            pst = m_pst.group(1)

            # Title: prefer <span class="titl">, fallback to <a title="">
            titl_el = a_tag.find("span", class_=lambda c: c and "titl" in c.split())
            if titl_el:
                title = titl_el.get_text(strip=True)
            else:
                title = a_tag.get("title", "").strip()

            # Date: <span class="info_dt">게시일:</span> → next <span class="info">
            listed_date_raw = ""
            for dt_span in a_tag.find_all("span", class_="info_dt"):
                if "게시일" in dt_span.get_text():
                    info_el = dt_span.find_next_sibling("span", class_="info")
                    if info_el:
                        listed_date_raw = info_el.get_text(strip=True)
                    break

            # Department (작성자)
            dept = ""
            for dt_span in a_tag.find_all("span", class_="info_dt"):
                if "작성자" in dt_span.get_text():
                    info_el = dt_span.find_next_sibling("span", class_="info")
                    if info_el:
                        dept = info_el.get_text(strip=True)
                    break

            # PDF download link from the list item (may have multiple duplicates)
            pdf_no = None
            for a_dl in div.find_all("a", class_="file_down"):
                dl_href = a_dl.get("href", "")
                m_no = re.search(r"no=(\d+)", dl_href)
                if m_no:
                    pdf_no = m_no.group(1)
                    break

            detail_url = f"https://www.krei.re.kr/krei/page/24?cmd=view&pst={pst}&pageIndex={page}"

            items.append({
                "pst": pst,
                "title": title,
                "listed_date_raw": listed_date_raw,
                "dept": dept,
                "pdf_no": pdf_no,
                "detail_url": detail_url,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, detail_url: str) -> dict | None:
        """Fetch and parse a detail page; return dict of fields or None."""
        raw = self._curl(detail_url)
        if not raw:
            return None

        soup = _make_soup(raw)
        if soup is None:
            return None

        result: dict = {}

        # Title
        h3 = soup.find("h3")
        result["title"] = h3.get_text(strip=True) if h3 else ""

        # Date from <span class="date">
        date_el = soup.find("span", class_="date")
        result["date_raw"] = date_el.get_text(strip=True) if date_el else ""

        # Category and author from <div class="item"> rows that contain <div class="tit">
        result["category"] = ""
        result["author"] = ""
        result["dept_detail"] = ""

        for item_div in soup.find_all("div", class_=lambda c: c and "item" in c.split()):
            tit_el = item_div.find("div", class_="tit")
            period_el = item_div.find("div", class_="period")
            if not tit_el or not period_el:
                continue
            label = tit_el.get_text(strip=True)
            value = period_el.get_text(strip=True)
            if "카테고리" in label:
                result["category"] = value
            elif "작성자" in label:
                result["dept_detail"] = value

        # Main content (abstract)
        main_cont = soup.find("div", class_="main_cont")
        if main_cont:
            result["abstract_html"] = str(main_cont)
            result["abstract"] = _strip_tags(str(main_cont))
        else:
            result["abstract_html"] = ""
            result["abstract"] = ""

        # PDF / 원문 files
        result["pdf_url"] = None
        result["original_filename"] = None

        for item_div in soup.find_all("div", class_=lambda c: c and "item" in c.split()):
            tit_el = item_div.find("div", class_="tit")
            if not tit_el:
                continue
            if "원문" not in tit_el.get_text():
                continue
            for a_file in item_div.find_all("a", class_="file_down"):
                href = a_file.get("href", "")
                if not href:
                    continue
                if not href.startswith("http"):
                    href = "https://www.krei.re.kr" + href
                filename = a_file.get_text(strip=True)
                if not filename:
                    title_attr = a_file.get("title", "")
                    filename = title_attr.replace(" 다운로드", "").strip()
                if result["pdf_url"] is None:
                    result["pdf_url"] = href
                    result["original_filename"] = filename or None
            break

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 보도자료 list pages and save to DB.

        Parameters
        ----------
        limit:
            Max papers to save (None = unlimited).
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget check
            if time.time() - start_time > self._MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall budget reached at page {page}. "
                      f"Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 1 and page > 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_items = self._fetch_list_page(page)

            if not list_items:
                print(f"[{self.site_id}] page {page}: no items returned. Stopping.")
                break

            new_on_page = 0
            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                detail_url = item["detail_url"]

                # URL dedup to detect silent pagination loops
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._RATE_SLEEP)
                    detail = self._fetch_detail(detail_url)

                    if detail is None:
                        print(f"[{self.site_id}] detail fetch failed for pst={item['pst']}, "
                              f"skipping")
                        continue

                    # Merge list-level and detail-level data
                    title = detail.get("title") or item.get("title") or ""
                    abstract = detail.get("abstract") or ""

                    if len(abstract) < 50:
                        print(f"[{self.site_id}] pst={item['pst']} abstract too short "
                              f"({len(abstract)} chars), skipping")
                        continue

                    pst = item["pst"]
                    listed_date = _parse_date(item.get("listed_date_raw") or
                                              detail.get("date_raw") or "")
                    published_date = _parse_date(detail.get("date_raw") or
                                                 item.get("listed_date_raw") or "")
                    dept = (detail.get("dept_detail") or item.get("dept") or "")
                    category = detail.get("category") or ""
                    pdf_url = detail.get("pdf_url")
                    original_filename = detail.get("original_filename")

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": pst,
                        "post_number": pst,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "authors": dept,
                        "publisher": "한국농촌경제연구원",
                        "department": dept,
                        "journal": None,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": None,
                        "keywords": None,
                        "category": category,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": item.get("listed_date_raw") or "",
                            "originalFilename": original_filename or "",
                            "pst": pst,
                            "pdf_no": item.get("pdf_no"),
                            "category": category,
                            "department": dept,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] pst={item.get('pst')} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen (dedup). "
                      f"Stopping.")
                break

        if page >= self._MAX_PAGES:
            print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
