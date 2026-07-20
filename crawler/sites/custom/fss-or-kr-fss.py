# -*- coding: utf-8 -*-
"""Crawler for FSS (금융감독원, Financial Supervisory Service) 보도자료 (Press Releases).

Target:  https://www.fss.or.kr/fss/bbs/B0000188/list.do?menuNo=200218
List:    GET list.do?menuNo=200218&pageIndex=N (a User-Agent header is
         required — without one the server falls back to an unrelated
         default board).
Detail:  GET /fss/bbs/B0000188/view.do?nttId={nttId}&menuNo=200218
Files:   /fss/cmmn/file/fileDown.do?menuNo=200218&atchFileId={id}&fileSn={n}
"""

import json
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

_BASE = "https://www.fss.or.kr"
_BBS_ID = "B0000188"
_MENU_NO = "200218"
_LIST_URL = f"{_BASE}/fss/bbs/{_BBS_ID}/list.do"
_VIEW_URL = f"{_BASE}/fss/bbs/{_BBS_ID}/view.do"
_PUBLISHER = "금융감독원"


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


def _clean_filename(raw: str) -> str:
    """Strip the trailing '(파일크기: NNNKB)' suffix from an attachment name."""
    return re.sub(r"\s*\(파일크기[^)]*\)\s*$", "", raw).strip()


class FssOrKrFssCrawler(BaseCrawler):
    """FSS (금융감독원) 보도자료 crawler.

    Class attributes satisfy BaseCrawler's abstract properties.
    """

    site_id = "fss-or-kr-fss"
    site_name = "Custom: fss-or-kr-fss"
    base_url = _BASE

    _MAX_PAGES = 200
    _MAX_SECS = 25 * 60  # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with exponential backoff; returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
            "-H", f"Referer: {_LIST_URL}?menuNo={_MENU_NO}",
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
        return self._curl_get(f"{_LIST_URL}?menuNo={_MENU_NO}&pageIndex={page}")

    def _parse_list_rows(self, html: str) -> list[dict]:
        """Extract row dicts (num, ntt_id, title, department, date, url) from a list page."""
        rows: list[dict] = []

        soup = _make_soup(html)
        if soup:
            try:
                blist = soup.find("div", class_="bd-list")
                table = blist.find("table") if blist else None
                tbody = table.find("tbody") if table else None
                trs = tbody.find_all("tr", recursive=False) if tbody else []
                for tr in trs:
                    tds = tr.find_all("td", recursive=False)
                    if len(tds) < 4:
                        continue
                    a = tds[1].find("a", href=True)
                    if not a:
                        continue
                    m = re.search(r"nttId=(\d+)", a["href"])
                    if not m:
                        continue
                    num = tds[0].get_text(strip=True)
                    title = a.get_text(strip=True)
                    department = tds[2].get_text(strip=True)
                    date_m = re.search(r"\d{4}-\d{2}-\d{2}", tds[3].get_text(strip=True))
                    date = date_m.group(0) if date_m else tds[3].get_text(strip=True)
                    rows.append({
                        "num": num,
                        "ntt_id": m.group(1),
                        "title": title,
                        "department": department,
                        "date": date,
                        "url": urljoin(_BASE, a["href"]),
                    })
            except Exception as exc:
                print(f"[{self.site_id}] list parse error (BeautifulSoup): {exc}")

        if rows:
            return rows

        # Regex fallback — pull nttId/title pairs directly.
        for m in re.finditer(
            rf'view\.do\?nttId=(\d+)[^"]*"[^>]*>([^<]+)</a>', html
        ):
            rows.append({
                "num": None,
                "ntt_id": m.group(1),
                "title": m.group(2).strip(),
                "department": "",
                "date": "",
                "url": f"{_VIEW_URL}?nttId={m.group(1)}&menuNo={_MENU_NO}",
            })
        return rows

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail_page(self, ntt_id: str) -> str | None:
        url = f"{_VIEW_URL}?nttId={ntt_id}&menuNo={_MENU_NO}"
        html = self._curl_get(url)
        if not html:
            return None
        if len(html) < 600:
            print(f"[{self.site_id}] nttId={ntt_id}: response too short, likely an error page")
            return None
        return html

    def _parse_detail(self, html: str) -> dict:
        """Parse all fields from a detail page; returns dict."""
        result: dict = {
            "title": "",
            "date": "",
            "department": "",
            "team": "",
            "body_text": "",
            "attachments": [],  # list of {"name": str, "url": str}
            "pdf_url": "",
            "original_filename": "",
        }

        soup = _make_soup(html)
        if not soup:
            return result

        try:
            view = soup.find("div", class_="bd-view") or soup

            h2 = view.find("h2", class_="subject")
            if h2:
                result["title"] = h2.get_text(strip=True)

            for dt in view.find_all("dt"):
                dd = dt.find_next_sibling("dd")
                if not dd:
                    continue
                key = dt.get_text(strip=True)
                if key == "등록일":
                    m = re.search(r"\d{4}-\d{2}-\d{2}", dd.get_text(strip=True))
                    result["date"] = m.group(0) if m else dd.get_text(strip=True)
                elif key == "담당부서":
                    result["department"] = dd.get_text(strip=True)
                elif key == "담당팀":
                    result["team"] = dd.get_text(strip=True)

            body = view.find("div", class_="dbdata")
            if body:
                result["body_text"] = body.get_text(" ", strip=True)

            for item in view.select("div.file-list__set__item"):
                a = item.find("a", href=lambda h: h and "fileDown.do" in h)
                if not a:
                    continue
                name_span = a.find("span", class_="name")
                raw_name = name_span.get_text(" ", strip=True) if name_span else ""
                name = _clean_filename(raw_name)
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

        return result

    # ------------------------------------------------------------------
    # Abstract construction
    # ------------------------------------------------------------------

    def _build_abstract(self, detail: dict) -> str:
        """Build an abstract from the body text plus supporting metadata.

        The body text alone is usually well over 100 characters for FSS
        press releases, but short releases are padded with attachment and
        department/date info so the abstract reliably clears the minimum.
        """
        parts: list[str] = []

        body = detail["body_text"].strip()
        if body:
            parts.append(body)

        if detail["attachments"]:
            names = " · ".join(a["name"] for a in detail["attachments"])
            parts.append("첨부파일: " + names)

        meta: list[str] = []
        if detail["department"]:
            meta.append(detail["department"])
        if detail["team"]:
            meta.append(detail["team"])
        if detail["date"]:
            meta.append(detail["date"])
        if meta:
            parts.append(f"{_PUBLISHER} · " + " · ".join(meta))

        parts.append(f"자료 분류: 보도자료 | 발행기관: {_PUBLISHER}")

        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl FSS 보도자료 pages and save records.

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

                    detail_html = self._fetch_detail_page(ntt_id)
                    if not detail_html:
                        print(f"[{self.site_id}] item {ntt_id} failed: no detail page")
                        continue

                    detail = self._parse_detail(detail_html)

                    abstract = self._build_abstract(detail)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {ntt_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    title = detail["title"] or row["title"] or f"금감원 보도자료 #{ntt_id}"
                    date = detail["date"] or row["date"] or None
                    department = detail["department"] or row["department"] or None

                    department_full = department
                    if detail["team"]:
                        department_full = (
                            f"{department};{detail['team']}" if department else detail["team"]
                        )

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": ntt_id,
                        "post_number": row["num"] if row["num"] else None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date,
                        "listed_date": date,
                        "authors": "",
                        "publisher": _PUBLISHER,
                        "department": department_full,
                        "journal": None,
                        "url": row["url"],
                        "pdf_url": detail["pdf_url"] or None,
                        "keywords": "보도자료",
                        "category": "보도자료",
                        "doi": None,
                        "original_filename": detail["original_filename"] or None,
                        "metadata": json.dumps(
                            {
                                "nttId": ntt_id,
                                "bbsId": _BBS_ID,
                                "menuNo": _MENU_NO,
                                "posted_date": date,
                                "originalFilename": detail["original_filename"] or None,
                                "journal_raw": None,
                                "series": None,
                                "volume": None,
                                "issue": None,
                                "post_number_raw": row["num"],
                                "department": department,
                                "team": detail["team"] or None,
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
