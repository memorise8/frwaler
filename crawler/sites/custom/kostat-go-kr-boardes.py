# -*- coding: utf-8 -*-
"""kostat.go.kr 국가통계포털 법령정보 게시판 (bid=1401) crawler.

Target: https://mods.go.kr/board.es?mid=a10403010000&bid=1401
  (www.kostat.go.kr now 301-redirects here — 통계청 reorganized into
   국가데이터처(MODS) circa 2026; board.es system + params unchanged,
   only the domain moved. curl doesn't follow redirects here, so point
   directly at the new domain.)

List endpoint  : GET /board.es?mid=a10403010000&bid=1401&nPage={n}
Detail endpoint: GET /board.es?mid=a10403010000&bid=1401&act=view&list_no={id}&nPage={p}
Download URL   : GET /boardDownload.es?bid=1401&list_no={id}&seq={seq}

Note: --tls-max 1.3 causes "Connection reset by peer" on this server; plain -sk works.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://mods.go.kr"
_BID = "1401"
_MID = "a10403010000"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _bs4(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("All BeautifulSoup parsers failed")


def _strip_tags(html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalize_date(raw: str) -> str:
    """Convert YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD to ISO YYYY-MM-DD."""
    if not raw:
        return ""
    raw = raw.strip()
    m = re.match(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return raw


# ---------------------------------------------------------------------------
# Network helper (curl without --tls-max 1.3 — resets on this server)
# ---------------------------------------------------------------------------

def _curl_get(url: str, params: dict | None = None, referer: str = "") -> str | None:
    """GET via curl with 3 retries and exponential backoff."""
    full_url = url
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        full_url = f"{url}?{qs}"

    cmd = [
        "curl", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(full_url)

    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            text = r.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception:
            pass
        if attempt < 2:
            wait = [1, 3, 9][attempt]
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KostatGoKrBoardesCrawler(BaseCrawler):
    """Crawler for 국가통계포털 법령정보 게시판 (kostat.go.kr board.es bid=1401)."""

    site_id = "kostat-go-kr-boardes"
    site_name = "Custom: kostat-go-kr-boardes"
    base_url = _BASE

    _LIST_URL = f"{_BASE}/board.es"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget guard
            if time.time() - start_time > self._TIMEOUT_SECS:
                elapsed = int(time.time() - start_time)
                print(f"[{self.site_id}] Time budget exceeded ({elapsed}s). Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Fetch list page
            list_html = _curl_get(
                self._LIST_URL,
                params={"mid": _MID, "bid": _BID, "nPage": str(page)},
                referer=self.base_url,
            )
            if not list_html:
                print(f"[{self.site_id}] List page {page} failed after retries. Stopping.")
                break

            # Extract list_nos via goView() onclick pattern (one per item, deduplicated)
            list_nos = list(dict.fromkeys(re.findall(r"goView\('(\d+)'\)", list_html)))
            if not list_nos:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_on_page = 0

            for list_no in list_nos:
                if limit is not None and saved >= limit:
                    break

                detail_url = (
                    f"{self._LIST_URL}?mid={_MID}&bid={_BID}"
                    f"&act=view&list_no={list_no}&nPage={page}"
                )

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                try:
                    paper = self._fetch_item(list_no, detail_url, page)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {list_no} failed: {exc}; continuing")
                    continue

                if paper is None:
                    continue

                abstract = paper.get("abstract", "") or ""
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] Skipping {list_no}: "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                self._save_paper(paper)
                saved += 1
                new_on_page += 1
                title = (paper.get("title") or "")[:60]
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title}")

                time.sleep(self._delay)

            if new_on_page == 0 and page > 1:
                print(f"[{self.site_id}] No new items on page {page} (all seen or skipped). Done.")
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {self._MAX_PAGES} pages. Logging and exiting.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------

    def _fetch_item(self, list_no: str, detail_url: str, page_num: int) -> dict | None:
        """Fetch and parse one detail page. Returns paper dict or None."""
        list_referer = f"{self._LIST_URL}?mid={_MID}&bid={_BID}&nPage={page_num}"
        detail_html = _curl_get(detail_url, referer=list_referer)
        if not detail_html:
            print(f"[{self.site_id}] Detail fetch failed for list_no={list_no}")
            return None

        try:
            dsoup = _bs4(detail_html)
        except Exception as exc:
            print(f"[{self.site_id}] Detail parse error {list_no}: {exc}")
            return None

        # --- Title: from sendSNS JS call (most reliable) ---
        title = ""
        sns_m = re.search(r"sendSNS\('facebook','([^']+)'\)", detail_html)
        if sns_m:
            title = sns_m.group(1).strip()
        if not title:
            # Fallback: extract from board_link anchor text
            link = dsoup.find("a", class_="board_link")
            if link:
                title = link.get_text(strip=True)

        # --- Metadata from <li><strong>Key</strong><span>Val</span></li> ---
        dept = ""
        author = ""
        phone = ""
        listed_date_raw = ""

        for li_el in dsoup.find_all("li"):
            strong = li_el.find("strong")
            span = li_el.find("span")
            if not strong or not span:
                continue
            key = strong.get_text(strip=True)
            val = span.get_text(strip=True)
            if key == "담당자":
                author = val
            elif key == "담당부서":
                dept = val
            elif key == "전화번호":
                phone = val
            elif key == "게시일":
                listed_date_raw = val

        listed_date = _normalize_date(listed_date_raw)

        # --- File attachments from bvf_list ---
        pdf_url = ""
        original_filename = ""
        file_names = []

        bvf_list = dsoup.find("ul", class_="bvf_list")
        if bvf_list:
            chosen_url = ""
            chosen_name = ""
            for a in bvf_list.find_all("a", class_="bvf_name"):
                href = a.get("href", "")
                fname = a.get_text(strip=True)
                if fname:
                    file_names.append(fname)
                if not href:
                    continue
                full_href = (_BASE + href) if href.startswith("/") else href
                lower = fname.lower()
                # Prefer PDF, then HWPX, then HWP, then first available
                if not chosen_url:
                    chosen_url = full_href
                    chosen_name = fname
                if lower.endswith(".pdf") and not chosen_name.lower().endswith(".pdf"):
                    chosen_url = full_href
                    chosen_name = fname
                    break
            pdf_url = chosen_url
            original_filename = chosen_name

        # --- Body text from bv_b > board_content ---
        body_text = ""
        bv_b = dsoup.find("div", class_="bv_b")
        if bv_b:
            bc = bv_b.find(class_="board_content")
            if bc:
                body_text = re.sub(r"\s+", " ", bc.get_text(" ", strip=True)).strip()
            else:
                body_text = re.sub(r"\s+", " ", bv_b.get_text(" ", strip=True)).strip()

        # --- Build abstract: body + filenames + metadata info ---
        parts = []
        if body_text:
            parts.append(body_text)
        if file_names:
            parts.append("[첨부: " + ", ".join(file_names) + "]")
        meta_parts = []
        if dept:
            meta_parts.append(f"담당부서: {dept}")
        if author:
            meta_parts.append(f"담당자: {author}")
        if listed_date_raw:
            meta_parts.append(f"게시일: {listed_date_raw}")
        if meta_parts:
            parts.append("[" + "; ".join(meta_parts) + "]")
        abstract = " ".join(parts).strip()

        # If still under 100 chars, prepend the title for length
        if len(abstract) < 100 and title:
            abstract = (title + " " + abstract).strip()

        paper = {
            "site_id": self.site_id,
            "external_id": list_no,
            "post_number": list_no,
            "title": title or f"(untitled-{list_no})",
            "abstract": abstract,
            "authors": author or None,
            "publisher": "통계청",
            "department": dept or None,
            "published_date": listed_date,
            "listed_date": listed_date,
            "url": detail_url,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename or None,
            "keywords": None,
            "category": "법령정보",
            "doi": None,
            "metadata": json.dumps(
                {
                    "list_no": list_no,
                    "phone": phone,
                    "posted_date": listed_date_raw,
                    "originalFilename": original_filename or None,
                    "file_names": file_names,
                },
                ensure_ascii=False,
            ),
        }
        return paper
