# -*- coding: utf-8 -*-
"""경찰청 보도자료 crawler (q_bbsCode=1002).

List:   GET https://www.police.go.kr/user/bbs/BD_selectBbsList.do
            ?q_bbsCode=1002&q_currPage=N&q_rowPerPage=10
Detail: GET https://www.police.go.kr/user/bbs/BD_selectBbs.do
            ?q_bbsCode=1002&q_bbscttSn=ID

The site uses a TMOSHCooKie bot-detection cookie (307 redirect on first hit).
We maintain a temp cookie jar and prime it before the crawl loop.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(raw: str):
    """Parse HTML with fallback chain html5lib → lxml → html.parser. Returns None on failure."""
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
# Crawler
# ---------------------------------------------------------------------------

class PoliceGoKrUserCrawler(BaseCrawler):
    """경찰청 보도자료 (q_bbsCode=1002) crawler."""

    site_id   = "police-go-kr-user"
    site_name = "Custom: police-go-kr-user"
    base_url  = "https://www.police.go.kr"

    _LIST_URL   = "https://www.police.go.kr/user/bbs/BD_selectBbsList.do"
    _DETAIL_URL = "https://www.police.go.kr/user/bbs/BD_selectBbs.do"
    _BBS_CODE   = "1002"
    _PAGE_SIZE  = 10
    _MAX_PAGES  = 200
    _MAX_SECS   = 25 * 60  # 25-minute wall-clock budget

    def __init__(self, db_conn, delay: float = 1.0):
        super().__init__(db_conn, delay)
        # Temp cookie jar — holds TMOSHCooKie across requests
        fd, self._cookie_file = tempfile.mkstemp(suffix=".txt", prefix="police_cookies_")
        os.close(fd)
        self._prime_cookies()

    def __del__(self):
        try:
            if hasattr(self, "_cookie_file") and os.path.exists(self._cookie_file):
                os.unlink(self._cookie_file)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _prime_cookies(self) -> None:
        """Fetch the list page once to collect the TMOSHCooKie redirect cookie."""
        url = f"{self._LIST_URL}?q_bbsCode={self._BBS_CODE}"
        cmd = [
            "curl", "-k", "--tlsv1.2",
            "-c", self._cookie_file,
            "-b", self._cookie_file,
            "-L", "--max-redirs", "5",
            "-A", self.USER_AGENT,
            "--max-time", "30",
            "-s", "-o", "/dev/null",
            url,
        ]
        try:
            subprocess.run(cmd, timeout=35, check=False)
        except Exception as exc:
            print(f"[{self.site_id}] Cookie priming failed (non-fatal): {exc}")

    def _curl(self, url: str, retries: int = 3) -> str | None:
        """GET via curl with persistent cookie jar. Returns decoded text or None."""
        cmd = [
            "curl", "-k", "--tlsv1.2",
            "-c", self._cookie_file,
            "-b", self._cookie_file,
            "-L", "--max-redirs", "5",
            "-A", self.USER_AGENT,
            "--max-time", "30",
            "-s",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                print(
                    f"[{self.site_id}] curl: empty response "
                    f"(attempt {attempt + 1}/{retries}) for {url}"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] curl error "
                    f"(attempt {attempt + 1}/{retries}): {exc}"
                )
            if attempt < retries - 1:
                wait = [1, 3, 9][attempt]
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # List page
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page: int) -> str | None:
        params = urllib.parse.urlencode({
            "q_bbsCode": self._BBS_CODE,
            "q_currPage": str(page),
            "q_rowPerPage": str(self._PAGE_SIZE),
        })
        return self._curl(f"{self._LIST_URL}?{params}")

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return list of {bbs_sn, title, listed_date, department} dicts."""
        items: list[dict] = []
        soup = _make_soup(html)
        if not soup:
            return items

        tbody = soup.find("tbody")
        if not tbody:
            return items

        for tr in tbody.find_all("tr", recursive=False):
            subject_td = tr.find("td", class_="subject")
            if not subject_td:
                continue
            link = subject_td.find("a")
            if not link:
                continue

            href = link.get("href", "")
            m = re.search(r"q_bbscttSn=(\w+)", href)
            if not m:
                onclick = link.get("onclick", "")
                m = re.search(r"opView\('(\w+)'\)", onclick)
            if not m:
                continue

            bbs_sn = m.group(1)
            title = link.get_text(strip=True)

            dept = ""
            listed_date = ""
            for td in tr.find_all("td"):
                classes = " ".join(td.get("class", []))
                if "subject" in classes:
                    continue
                txt = td.get_text(strip=True)
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", txt):
                    listed_date = txt
                elif (txt
                      and not re.fullmatch(r"\d+", txt)
                      and "파일" not in txt
                      and len(txt) > 3
                      and not dept):
                    dept = txt

            items.append({
                "bbs_sn": bbs_sn,
                "title": title,
                "listed_date": listed_date,
                "department": dept,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _fetch_detail(self, bbs_sn: str) -> str | None:
        params = urllib.parse.urlencode({
            "q_bbsCode": self._BBS_CODE,
            "q_bbscttSn": bbs_sn,
        })
        return self._curl(f"{self._DETAIL_URL}?{params}")

    def _extract_abstract(self, soup, html: str) -> str:
        """Pull the best available body text from a detail page."""
        abstract = ""

        # Primary: board_mobile div (always rendered; includes title prefix + boilerplate)
        if soup:
            mob = soup.find(class_="board_mobile")
            if mob:
                abstract = mob.get_text(separator=" ", strip=True)

        # Secondary: board_pc with iframes/scripts removed
        if (not abstract or len(abstract) < 50) and soup:
            pc = soup.find(class_="board_pc")
            if pc:
                for bad in pc.find_all(["script", "style", "iframe", "noscript"]):
                    bad.decompose()
                t = pc.get_text(separator=" ", strip=True)
                if len(t) > len(abstract):
                    abstract = t

        # Tertiary: any large <td> block
        if (not abstract or len(abstract) < 50) and soup:
            candidates = [td.get_text(separator=" ", strip=True) for td in soup.find_all("td")]
            best = max(candidates, key=len, default="")
            if len(best) > len(abstract):
                abstract = best

        # Regex fallback from raw HTML
        if not abstract or len(abstract) < 50:
            m = re.search(r'class="board_mobile"[^>]*>(.*?)</div>', html, re.DOTALL)
            if m:
                t = _strip_html(m.group(1))
                if len(t) > len(abstract):
                    abstract = t

        return re.sub(r"\s+", " ", abstract).strip()

    def _parse_detail(self, html: str, item: dict) -> dict | None:
        """Parse a detail page into a paper dict. Returns None on hard failure."""
        bbs_sn = item["bbs_sn"]

        try:
            soup = _make_soup(html)
        except Exception:
            soup = None

        # Title — try h1.page-header first, then fall back to list title
        title = item.get("title", "")
        if soup:
            for sel in [
                lambda s: s.find(class_="page-header"),
                lambda s: s.find("h1"),
                lambda s: s.find("h2"),
            ]:
                try:
                    el = sel(soup)
                except Exception:
                    el = None
                if el:
                    t = el.get_text(strip=True)
                    if t and 5 < len(t) < 300:
                        title = t
                        break

        # Metadata rows: 등록일시, 부서명
        pub_date = ""
        dept = item.get("department", "")
        if soup:
            for tr in soup.find_all("tr"):
                th = tr.find("th")
                td = tr.find("td")
                if not th or not td:
                    continue
                key = th.get_text(strip=True)
                val = td.get_text(strip=True)
                if "등록일시" in key:
                    m = re.match(r"(\d{4}-\d{2}-\d{2})", val)
                    pub_date = m.group(1) if m else val[:10]
                elif "부서명" in key and not dept:
                    dept = val

        # Abstract
        abstract = self._extract_abstract(soup, html)

        # File attachments — prefer PDF
        pdf_url: str | None = None
        original_filename: str | None = None

        if soup:
            file_ul = soup.find("ul", class_="file-download")
            if file_ul:
                # Pass 1: PDF files
                for li in file_ul.find_all("li"):
                    has_pdf_icon = bool(
                        li.find("span", class_=lambda c: c and "ico-pdf" in c)
                    )
                    a = li.find("a", href=True)
                    if has_pdf_icon and a and not pdf_url:
                        href = a["href"]
                        pdf_url = (self.base_url + href) if href.startswith("/") else href
                        original_filename = a.get_text(strip=True)
                # Pass 2: any downloadable file
                if not pdf_url:
                    for li in file_ul.find_all("li"):
                        a = li.find("a", href=True)
                        if a and "ND_fileDownload" in a.get("href", ""):
                            href = a["href"]
                            pdf_url = (self.base_url + href) if href.startswith("/") else href
                            original_filename = a.get_text(strip=True)
                            break

        # Regex fallback for PDF URL
        if not pdf_url:
            m = re.search(
                r'ico-pdf.*?href="(/component/file/ND_fileDownload\.do[^"]+)"[^>]*>([^<]+)',
                html, re.DOTALL,
            )
            if m:
                pdf_url = self.base_url + m.group(1)
                original_filename = m.group(2).strip()

        detail_url = (
            f"{self._DETAIL_URL}?q_bbsCode={self._BBS_CODE}&q_bbscttSn={bbs_sn}"
        )

        return {
            "site_id": self.site_id,
            "external_id": bbs_sn,
            "post_number": bbs_sn,
            "title": title or item.get("title", ""),
            "abstract": abstract,
            "published_date": pub_date or item.get("listed_date", ""),
            "listed_date": item.get("listed_date", ""),
            "authors": "",
            "publisher": "경찰청",
            "department": dept,
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": "보도자료",
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "bbscttSn": bbs_sn,
                    "bbsCode": self._BBS_CODE,
                    "posted_date": item.get("listed_date", ""),
                    "originalFilename": original_filename,
                },
                ensure_ascii=False,
            ),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 경찰청 보도자료 list, save up to `limit` items (None = unlimited)."""
        saved = 0
        seen_urls: set[str] = set()
        limit_label = str(limit) if limit is not None else "∞"
        t0 = time.monotonic()
        page = 1

        while True:
            # ── Guards ──
            if time.monotonic() - t0 > self._MAX_SECS:
                print(f"[{self.site_id}] Wall-clock budget ({self._MAX_SECS}s) exceeded. Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping."
                )
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            # ── List page ──
            list_html = self._fetch_list_page(page)
            if not list_html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            list_items = self._parse_list_page(list_html)
            if not list_items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            # Pagination-loop detection
            first_url = (
                f"{self._DETAIL_URL}?q_bbsCode={self._BBS_CODE}"
                f"&q_bbscttSn={list_items[0]['bbs_sn']}"
            )
            if page > 1 and first_url in seen_urls:
                print(f"[{self.site_id}] Pagination loop detected at page {page}. Stopping.")
                break

            any_new = False
            for item in list_items:
                if limit is not None and saved >= limit:
                    break

                bbs_sn = item["bbs_sn"]
                detail_url = (
                    f"{self._DETAIL_URL}?q_bbsCode={self._BBS_CODE}&q_bbscttSn={bbs_sn}"
                )

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                any_new = True

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail(bbs_sn)
                    if not detail_html:
                        print(f"[{self.site_id}] item {bbs_sn}: fetch failed (empty response)")
                        continue

                    paper = self._parse_detail(detail_html, item)
                    if not paper:
                        print(f"[{self.site_id}] item {bbs_sn}: parse returned None")
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {bbs_sn}: skipped "
                            f"(abstract too short: {len(abstract)} chars)"
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
                    print(f"[{self.site_id}] item {bbs_sn}: failed: {exc}")
                    continue

            if not any_new:
                print(f"[{self.site_id}] No new items on page {page}. Done.")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
