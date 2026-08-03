# -*- coding: utf-8 -*-
"""새만금개발청 언론보도 crawler.

Starting URL: https://www.saemangeum.go.kr/sda/brd/list.do?key=2009074409621
Board key: 2009074409621  (bbsSn=6)
List pagination: GET /sda/brd/list.do?key=2009074409621&pageIndex=N
Detail:          GET /sda/brd/view.do?key=2009074409621&nttSn=XXXXX
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BOARD_KEY = "2009074409621"
_BASE = "https://www.saemangeum.go.kr"
_LIST_URL = f"{_BASE}/sda/brd/list.do?key={_BOARD_KEY}"
_VIEW_BASE = f"{_BASE}/sda/brd/view.do"
_PUBLISHER = "새만금개발청"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_WALL_MINUTES = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl (TLS-max 1.3) with exponential-backoff retries."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout
            if raw:
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")
        except Exception as exc:
            print(f"[saemangeum-go-kr-sda] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = 3 ** attempt  # 1s, 3s, 9s
            time.sleep(wait)
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalize whitespace to plain text."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&#\d+;", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> str:
    """Convert '2026.05.14' → '2026-05-14'."""
    if not raw:
        return ""
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", raw.strip())
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return raw.strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class SaemangeumSdaCrawler(BaseCrawler):
    """새만금개발청 언론보도 crawler."""

    site_id = "saemangeum-go-kr-sda"
    site_name = "Custom: saemangeum-go-kr-sda"
    base_url = "https://www.saemangeum.go.kr"

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.monotonic()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            # Wall-clock budget
            elapsed_min = (time.monotonic() - start_time) / 60
            if elapsed_min >= _WALL_MINUTES:
                print(
                    f"[saemangeum-go-kr-sda] Wall-clock budget reached "
                    f"({elapsed_min:.1f}m). Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            if page == 1 or page % 10 == 0:
                print(f"[saemangeum-go-kr-sda] page {page}: saved {saved}/{limit_str}")

            if page == _MAX_PAGES:
                print(f"[saemangeum-go-kr-sda] Reached safety cap of {_MAX_PAGES} pages.")

            url = f"{_LIST_URL}&pageIndex={page}"
            raw = _curl_get(url)
            if not raw:
                print(f"[saemangeum-go-kr-sda] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[saemangeum-go-kr-sda] Parse error on list page {page}: {exc}. Stopping.")
                break

            items = self._parse_list_page(soup)
            if not items:
                print(f"[saemangeum-go-kr-sda] No items on page {page}. Done.")
                break

            new_items = [it for it in items if it["view_url"] not in seen_urls]
            if not new_items:
                print(
                    f"[saemangeum-go-kr-sda] All items on page {page} already seen "
                    f"(loop detected). Done."
                )
                break

            for item in new_items:
                if limit is not None and saved >= limit:
                    break
                seen_urls.add(item["view_url"])
                try:
                    n = self._crawl_item(item)
                    saved += n
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[saemangeum-go-kr-sda] item {item.get('ntt_sn')} failed: {exc}"
                    )
                    continue
                time.sleep(self._delay)

        print(f"[saemangeum-go-kr-sda] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List-page parser
    # ------------------------------------------------------------------

    def _parse_list_page(self, soup) -> list:
        """Return list of item dicts parsed from a list page."""
        items = []
        rows = soup.select("tbody tr")
        for row in rows:
            try:
                td_title = row.find("td", title=True)
                if not td_title:
                    continue

                title = td_title.get("title", "").strip()

                # nttSn from onclick="goView('XXXXX', '')"
                a_link = td_title.find("a", class_="tit")
                if not a_link:
                    continue
                onclick = a_link.get("onclick", "")
                m = re.search(r"goView\('(\d+)'", onclick)
                if not m:
                    continue
                ntt_sn = m.group(1)

                if not title:
                    title = a_link.get_text(strip=True)

                # Board row number (post_number) from <th scope="row">
                th = row.find("th", {"scope": "row"})
                post_number = th.get_text(strip=True) if th else None

                # Listed date from <td class="mbinline">
                date_td = row.find("td", class_="mbinline")
                raw_date = date_td.get_text(strip=True) if date_td else ""
                listed_date = _parse_date(raw_date)

                # Department: 3rd <td> (title=0, file=1, dept=2)
                all_tds = row.find_all("td")
                dept = ""
                if len(all_tds) >= 3:
                    dept = re.sub(r"\s+", " ", all_tds[2].get_text(strip=True)).strip()

                view_url = f"{_VIEW_BASE}?key={_BOARD_KEY}&nttSn={ntt_sn}"
                items.append({
                    "ntt_sn": ntt_sn,
                    "post_number": post_number,
                    "title": title,
                    "listed_date": listed_date,
                    "department": dept,
                    "view_url": view_url,
                })
            except Exception as exc:
                print(f"[saemangeum-go-kr-sda] Row parse error: {exc}")
                continue
        return items

    # ------------------------------------------------------------------
    # Detail-page fetcher
    # ------------------------------------------------------------------

    def _crawl_item(self, item: dict) -> int:
        """Fetch detail page, save paper. Returns 1 on success, 0 on skip/error."""
        ntt_sn = item["ntt_sn"]
        view_url = item["view_url"]

        raw = _curl_get(view_url)
        if not raw:
            print(f"[saemangeum-go-kr-sda] Detail fetch failed for nttSn={ntt_sn}")
            return 0

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[saemangeum-go-kr-sda] Detail parse error nttSn={ntt_sn}: {exc}")
            return 0

        # Title
        h4 = soup.select_one(".viewtit h4")
        title = h4.get_text(strip=True) if h4 else item["title"]
        if not title:
            title = item["title"]

        # Date — ".date" div contains year + "MM.DD" span
        listed_date = item["listed_date"]
        date_div = soup.select_one(".viewtit .date")
        if date_div:
            raw_date = re.sub(r"\s+", "", date_div.get_text())
            m = re.search(r"(\d{4})(\d{2})\.(\d{2})", raw_date)
            if m:
                listed_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        # Author & department from .dpname spans
        author = ""
        dept = item["department"]
        prev_label = ""
        for span in soup.select(".viewtit .depart .dpname"):
            b = span.find("b")
            if b:
                prev_label = b.get_text(strip=True)
            else:
                val = span.get_text(strip=True)
                if not val:
                    continue
                if prev_label == "작성자":
                    author = val
                elif prev_label == "주관부서 담당자":
                    # Value may contain "부서명 담당자 전화번호" — take first token
                    parts = re.split(r"\s+", re.sub(r" ", " ", val).strip())
                    if parts:
                        dept = parts[0]

        # Body → abstract
        article = soup.select_one("article.view_article")
        abstract = ""
        if article:
            abstract = _strip_html(str(article))

        if len(abstract) < 50:
            print(
                f"[saemangeum-go-kr-sda] Skipping nttSn={ntt_sn}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return 0

        # PDF attachment (prefer .isi_dw4 = PDF class)
        pdf_url = None
        original_filename = None
        pdf_link = soup.select_one(".download_wrap a.isi_dw4")
        if pdf_link:
            href = pdf_link.get("href", "")
            if href and not href.startswith("javascript"):
                pdf_url = (
                    f"{_BASE}{href}" if href.startswith("/") else href
                )
                fname_raw = pdf_link.get_text(" ", strip=True)
                m = re.match(r"^(.+?)\s*\(", fname_raw)
                fname = m.group(1).strip() if m else fname_raw.strip()
                if fname and not fname.lower().endswith(".pdf"):
                    fname += ".pdf"
                original_filename = fname or None

        paper = {
            "site_id": self.site_id,
            "external_id": ntt_sn,
            "post_number": post_number_str(item["post_number"]),
            "title": title,
            "abstract": abstract,
            "published_date": listed_date,
            "listed_date": listed_date,
            "authors": author or None,
            "publisher": _PUBLISHER,
            "department": dept or None,
            "url": view_url,
            "pdf_url": pdf_url,
            "keywords": None,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "posted_date": listed_date,
                    "nttSn": ntt_sn,
                    "post_number": item["post_number"],
                    "department": dept or None,
                    "originalFilename": original_filename,
                },
                ensure_ascii=False,
            ),
        }

        self._save_paper(paper)
        print(f"[saemangeum-go-kr-sda] Saved [{ntt_sn}] {title[:60]}")
        return 1


def post_number_str(val) -> str | None:
    """Return post_number as numeric string if possible, else None."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s.isdigit() else None
