# -*- coding: utf-8 -*-
"""Crawler for BOK (한국은행, Bank of Korea) 뉴스/자료 (News/Data) board.

Target:  https://www.bok.or.kr/portal/singl/newsData/list.do?menuNo=201150&depth2=200038
List:    GET /portal/singl/newsData/listCont.do?pageIndex=N&menuNo=201150&depth2=200038&...
         (this is the AJAX fragment endpoint the list page itself calls to
         populate ``#bbsList`` — much lighter than the full list.do wrapper
         page and avoids having to execute any client-side JS).
Detail:  GET {href} taken verbatim from each list row's ``<a class="title">``
         — the board code (P0001726, B0000502, ...) and ``menuNo`` vary per
         item/category, so detail URLs are never reconstructed manually.
Files:   /fileSrc/portal/{atchFileId}/{seq}/{hash}.{ext} (direct static path,
         filename lives in the ``<a title="...">`` attribute on the detail
         page — no separate fileDown.do redirect for this board).
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

_BASE = "https://www.bok.or.kr"
_LIST_CONT_URL = f"{_BASE}/portal/singl/newsData/listCont.do"
_LIST_URL = f"{_BASE}/portal/singl/newsData/list.do"
_PUBLISHER = "한국은행"

# Query params shared by every listCont.do call, taken from the given
# starting URL. Only ``pageIndex`` changes between pages.
_LIST_PARAMS_BASE = {
    "targetDepth": "",
    "menuNo": "201150",
    "syncMenuChekKey": "1",
    "depthSubMain": "",
    "subMainAt": "",
    "searchCnd": "1",
    "searchKwd": "",
    "depth2": "200038",
    "date": "",
    "sdate": "",
    "edate": "",
    "sort": "1",
    "pageUnit": "10",
}


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib -> lxml -> html.parser."""
    if not _BS4_OK:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _dot_date_to_iso(raw: str) -> str | None:
    """Convert '2026.07.17' style dates to ISO 'YYYY-MM-DD'."""
    m = re.search(r"(\d{4})[.\-](\d{2})[.\-](\d{2})", raw or "")
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


class BokOrKrPortalCrawler(BaseCrawler):
    """BOK (한국은행) 뉴스/자료 crawler.

    Class attributes satisfy BaseCrawler's abstract properties.
    """

    site_id = "bok-or-kr-portal"
    site_name = "Custom: bok-or-kr-portal"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with exponential backoff (1s, 3s, 9s); returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
            "-H", f"Referer: {_LIST_URL}?menuNo=201150",
            url,
        ]
        for attempt in range(retries):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                text = res.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 3 ** attempt
                print(
                    f"[{self.site_id}] empty GET (attempt {attempt + 1}/{retries}),"
                    f" retry in {wait}s: {url}"
                )
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(
                    f"[{self.site_id}] curl GET error (attempt {attempt + 1}/{retries}):"
                    f" {exc}, retry in {wait}s"
                )
                time.sleep(wait)
        print(f"[{self.site_id}] GET failed after {retries} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> str | None:
        params = dict(_LIST_PARAMS_BASE)
        params["pageIndex"] = str(page)
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        return self._curl_get(f"{_LIST_CONT_URL}?{qs}")

    def _parse_list_rows(self, html: str) -> list[dict]:
        """Extract row dicts (ntt_id, title, category, department, date, url) from a list page."""
        rows: list[dict] = []

        soup = _make_soup(html)
        if soup:
            try:
                for li in soup.select("li.bbsRowCls"):
                    a = li.select_one("div.set a[href]")
                    if not a or not a.get("href"):
                        continue
                    m = re.search(r"nttId=(\d+)", a["href"])
                    if not m:
                        continue
                    ntt_id = m.group(1)
                    title = a.get_text(" ", strip=True)

                    cat_el = li.select_one("span.t1")
                    category = cat_el.get_text(strip=True) if cat_el else ""

                    dept_el = li.select_one("span.depart")
                    department = ""
                    if dept_el:
                        sr = dept_el.find("span", class_="sr-only")
                        if sr:
                            sr.extract()
                        department = dept_el.get_text(strip=True)

                    date_el = li.select_one("span.date")
                    date_raw = ""
                    if date_el:
                        sr = date_el.find("span", class_="sr-only")
                        if sr:
                            sr.extract()
                        date_raw = date_el.get_text(strip=True)

                    rows.append({
                        "ntt_id": ntt_id,
                        "title": title,
                        "category": category,
                        "department": department,
                        "date_raw": date_raw,
                        "date": _dot_date_to_iso(date_raw),
                        "url": urljoin(_BASE, a["href"]),
                    })
            except Exception as exc:
                print(f"[{self.site_id}] list parse error (BeautifulSoup): {exc}")

        if rows:
            return rows

        # Regex fallback — pull nttId/href/title triples directly.
        for m in re.finditer(
            r'<a href="([^"]*nttId=\d+[^"]*)"[^>]*class="title">\s*(?:<!--.*?-->\s*)?([^<]+)</a>',
            html, re.S,
        ):
            href, title = m.group(1), m.group(2).strip()
            nm = re.search(r"nttId=(\d+)", href)
            if not nm:
                continue
            rows.append({
                "ntt_id": nm.group(1),
                "title": title,
                "category": "",
                "department": "",
                "date_raw": "",
                "date": None,
                "url": urljoin(_BASE, href.replace("&amp;", "&")),
            })
        return rows

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail_page(self, url: str) -> str | None:
        html = self._curl_get(url)
        if not html:
            return None
        if len(html) < 600:
            print(f"[{self.site_id}] detail fetch too short, likely an error page: {url}")
            return None
        return html

    def _parse_detail(self, html: str) -> dict:
        """Parse all fields from a detail page; returns dict."""
        result: dict = {
            "title": "",
            "date": None,
            "date_raw": "",
            "category": "",
            "department": "",
            "keywords": [],
            "body_text": "",
            "attachments": [],  # list of {"name": str, "url": str}
            "pdf_url": "",
            "original_filename": "",
        }

        soup = _make_soup(html)
        if not soup:
            return result

        try:
            view = soup.select_one("div.bd-view") or soup

            h2 = view.select_one("h2.subject")
            if h2:
                result["title"] = h2.get_text(strip=True)

            dl = view.select_one("dl.dataInfo")
            if dl:
                for dt in dl.find_all("dt"):
                    dd = dt.find_next_sibling("dd")
                    if not dd:
                        continue
                    key = dt.get_text(strip=True)
                    if key == "구분":
                        result["category"] = dd.get_text(strip=True)
                    elif key == "등록일":
                        raw = dd.get_text(strip=True)
                        result["date_raw"] = raw
                        result["date"] = _dot_date_to_iso(raw)

            kw = view.select_one("dl.keyword dd")
            if kw:
                result["keywords"] = [
                    a.get_text(strip=True) for a in kw.select("a") if a.get_text(strip=True)
                ]

            dept = view.select_one("dl.type2 dd.depart")
            if dept:
                result["department"] = dept.get_text(strip=True)

            body = view.select_one("div.dbdata")
            if body:
                result["body_text"] = body.get_text(" ", strip=True)

            for a in view.select("dl.down li > a[href]"):
                name = a.get("title") or a.get_text(strip=True)
                if not name:
                    continue
                result["attachments"].append({
                    "name": name,
                    "url": urljoin(_BASE, a["href"]),
                })

        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup detail parse error: {exc}")

        for att in result["attachments"]:
            if att["name"].lower().endswith(".pdf"):
                result["pdf_url"] = att["url"]
                result["original_filename"] = att["name"]
                break
        if not result["original_filename"] and result["attachments"]:
            result["original_filename"] = result["attachments"][0]["name"]

        return result

    # ------------------------------------------------------------------
    # Abstract construction
    # ------------------------------------------------------------------

    def _build_abstract(self, detail: dict, row: dict) -> str:
        """Build an abstract from body text plus supporting metadata.

        Many BOK board items (e.g. 의사록/minutes) carry almost no body
        text of their own — the substance lives in an attached PDF/HWP —
        so the abstract is padded with attachment names, category,
        department, and keyword info to reliably clear the minimum length.
        """
        parts: list[str] = []

        body = detail["body_text"].strip()
        if body:
            parts.append(body)

        if detail["attachments"]:
            names = " · ".join(a["name"] for a in detail["attachments"])
            parts.append("첨부파일: " + names)

        category = detail["category"] or row.get("category") or ""
        meta: list[str] = []
        if category:
            meta.append(category)
        if detail["department"]:
            meta.append(detail["department"])
        if detail["date_raw"]:
            meta.append(detail["date_raw"])
        if meta:
            parts.append(f"{_PUBLISHER} · " + " · ".join(meta))

        if detail["keywords"]:
            parts.append("키워드: " + ", ".join(detail["keywords"]))

        parts.append(f"자료 분류: {category or '뉴스/자료'} | 발행기관: {_PUBLISHER}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl BOK 뉴스/자료 pages and save records.

        Parameters
        ----------
        limit:
            Max records to save. ``None`` = unlimited.
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        page = 1
        lim_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_SECS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}, stopping")
                break

            # Limit satisfied
            if limit is not None and saved >= limit:
                break

            # Safety page cap
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached")
                break

            # Progress log every 10 pages
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            # --- Fetch list page ---
            list_html = self._fetch_list_page(page)
            if not list_html:
                print(f"[{self.site_id}] page {page}: fetch failed, stopping")
                break

            rows = self._parse_list_rows(list_html)
            if not rows:
                print(f"[{self.site_id}] page {page}: no items found, done")
                break

            # URL deduplication — detects silent pagination loops (paginator
            # that silently wraps back to page 1 returns only seen URLs).
            new_rows = [r for r in rows if r["url"] not in seen_urls]
            if not new_rows:
                print(
                    f"[{self.site_id}] page {page}: all {len(rows)} items already seen, done"
                )
                break

            # --- Process each new item ---
            for row in new_rows:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(row["url"])
                ntt_id = row["ntt_id"]

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail_page(row["url"])
                    if not detail_html:
                        print(f"[{self.site_id}] item {ntt_id} failed: no detail page")
                        continue

                    detail = self._parse_detail(detail_html)

                    abstract = self._build_abstract(detail, row)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {ntt_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    title = detail["title"] or row["title"] or f"한국은행 뉴스/자료 #{ntt_id}"
                    date = detail["date"] or row["date"] or None
                    department = detail["department"] or row["department"] or None
                    category = detail["category"] or row["category"] or None

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": ntt_id,
                        "post_number": ntt_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date,
                        "listed_date": row["date"] or date,
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": department,
                        "journal": None,
                        "url": row["url"],
                        "pdf_url": detail["pdf_url"] or None,
                        "keywords": ", ".join(detail["keywords"]) if detail["keywords"] else None,
                        "category": category,
                        "doi": None,
                        "original_filename": detail["original_filename"] or None,
                        "metadata": json.dumps(
                            {
                                "nttId": ntt_id,
                                "menuNo": _LIST_PARAMS_BASE["menuNo"],
                                "depth2": _LIST_PARAMS_BASE["depth2"],
                                "posted_date": row["date_raw"] or detail["date_raw"] or None,
                                "originalFilename": detail["original_filename"] or None,
                                "journal_raw": None,
                                "series": None,
                                "volume": None,
                                "issue": None,
                                "category_list_raw": row["category"] or None,
                                "category_detail_raw": detail["category"] or None,
                                "department_raw": department,
                                "keywords_list": detail["keywords"],
                                "attachments": detail["attachments"],
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {ntt_id} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
