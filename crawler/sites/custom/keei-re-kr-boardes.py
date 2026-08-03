# -*- coding: utf-8 -*-
"""KEEI 에너지경제연구원 연구보고서 crawler.

Target: https://www.keei.re.kr/board.es?mid=a10101010000&bid=0001

List  : GET /board.es?mid=a10101010000&bid=0001&nPage={n}
Detail: GET /board.es?mid=a10101010000&bid=0001&act=view&list_no={id}&nPage=1
PDF   : GET /boardDownload.es?bid=0001&list_no={id}&seq=1
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.keei.re.kr"
_MID = "a10101010000"
_BID = "0001"
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


def _text(el) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _strip_tags(html_str: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html_str)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&[a-zA-Z#\d]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# curl GET with retries (SSL workaround for Korean gov sites)
# ---------------------------------------------------------------------------

def _curl_get(url: str, referer: str = "") -> str | None:
    """GET via curl --tls-max 1.3 with 3 retries (1s, 3s, 9s backoff)."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
        "-H", f"User-Agent: {_UA}",
        "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
    ]
    if referer:
        cmd += ["-H", f"Referer: {referer}"]
    cmd.append(url)

    for attempt in range(3):
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=35)
            text = r.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
        except Exception:
            pass
        if attempt < 2:
            time.sleep([1, 3, 9][attempt])
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class KeeiReKrBoardesCrawler(BaseCrawler):
    """Crawler for KEEI 에너지경제연구원 연구보고서, bid=0001."""

    site_id = "keei-re-kr-boardes"
    site_name = "Custom: keei-re-kr-boardes"
    base_url = _BASE

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _TIMEOUT_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25 minutes
    _MIN_ABSTRACT = 50         # skip items shorter than this

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock budget check
            if time.time() - start_time > self._TIMEOUT_SECS:
                elapsed = int(time.time() - start_time)
                print(f"[{self.site_id}] Time budget exceeded ({elapsed}s). Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Reached safety cap of {self._MAX_PAGES} pages.")

            list_url = f"{_BASE}/board.es?mid={_MID}&bid={_BID}&nPage={page}"
            list_html = _curl_get(list_url, referer=self.base_url)
            if not list_html:
                print(f"[{self.site_id}] List page {page} failed after retries. Stopping.")
                break

            try:
                soup = _bs4(list_html)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error list page {page}: {exc}. Skipping.")
                continue

            report_list = soup.find("ul", class_="report_list")
            if not report_list:
                print(f"[{self.site_id}] No report_list on page {page}. Done.")
                break

            items = report_list.find_all("li", recursive=False)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_on_page = 0

            for li in items:
                if limit is not None and saved >= limit:
                    break

                list_no = "?"
                try:
                    anchor = li.find("a", href=re.compile(r"act=view"))
                    if not anchor:
                        continue

                    href = anchor.get("href", "")
                    m = re.search(r"list_no=(\d+)", href)
                    if not m:
                        continue
                    list_no = m.group(1)

                    detail_url = (
                        f"{_BASE}/board.es?mid={_MID}&bid={_BID}"
                        f"&act=view&list_no={list_no}&nPage=1"
                    )

                    # URL dedup — detects paginator loop-back
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    # --- list-level fields ---
                    title = _text(anchor)

                    cate_el = li.find("span", class_="cate")
                    category = _text(cate_el)

                    label_el = li.find("span", class_="label")
                    report_type = _text(label_el)

                    date_el = li.find("span", class_="date")
                    raw_date = _text(date_el)
                    published_date = _norm_date(raw_date)

                    auth_el = li.find("span", class_="auth")
                    authors_list = _parse_authors(_text(auth_el))

                    # PDF from list-level "바로보기" button
                    pdf_url = ""
                    pdf_btn = li.find("a", href=re.compile(r"pdfOpen|boardDownload"))
                    if pdf_btn:
                        ph = pdf_btn.get("href", "")
                        pdf_url = ph if ph.startswith("http") else f"{_BASE}{ph}"
                    if not pdf_url:
                        pdf_url = f"{_BASE}/boardDownload.es?bid={_BID}&list_no={list_no}&seq=1"

                    # --- detail page ---
                    time.sleep(self._delay)
                    detail_html = _curl_get(detail_url, referer=list_url)

                    abstract = ""
                    series = ""

                    if detail_html:
                        try:
                            dsoup = _bs4(detail_html)

                            # Abstract: bg_txt_box is the summary section
                            bg_box = dsoup.find("div", class_="bg_txt_box")
                            if bg_box:
                                abstract = _strip_tags(str(bg_box))

                            # Richer metadata from view_top info list
                            for ili in dsoup.select("ul.info > li"):
                                strong = ili.find("strong")
                                span = ili.find("span")
                                if not strong or not span:
                                    continue
                                key = _text(strong)
                                val = _text(span)
                                if key == "저자" and val:
                                    authors_list = _parse_authors(val)
                                elif key == "총서사항" and val:
                                    series = val
                                elif key == "발행일" and val:
                                    d = _norm_date(val)
                                    if d:
                                        published_date = d

                            # Category from detail (more specific than list)
                            cate_tags = dsoup.select("ul.cate > li")
                            if cate_tags:
                                category = " > ".join(_text(t) for t in cate_tags)

                            # PDF from detail download button (preferred over list)
                            dl_btn = dsoup.find("a", href=re.compile(r"boardDownload"))
                            if dl_btn:
                                dh = dl_btn.get("href", "")
                                if dh:
                                    pdf_url = dh if dh.startswith("http") else f"{_BASE}{dh}"

                        except Exception as exc:
                            print(f"[{self.site_id}] detail parse error {list_no}: {exc}")

                    # Skip if abstract too short
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Skipping {list_no}: "
                            f"abstract too short ({len(abstract)} chars): {title[:50]}"
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": list_no,
                        "title": title,
                        "authors": json.dumps(authors_list, ensure_ascii=False),
                        "abstract": abstract,
                        "category": category,
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "에너지경제연구원",
                        "metadata": json.dumps({
                            "report_type": report_type,
                            "series": series,
                            "list_no": list_no,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {list_no} failed: {exc}; continuing")
                    continue

            # All new items on this page were already seen → paginator looped
            if new_on_page == 0 and page > 1:
                print(f"[{self.site_id}] No new URLs on page {page} (all seen). Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _norm_date(raw: str) -> str:
    """Normalize various date formats to YYYY-MM-DD."""
    if not raw:
        return ""
    m = re.search(r"(\d{4})[./\-](\d{2})[./\-](\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


def _parse_authors(raw: str) -> list[str]:
    """Split comma/semicolon-separated author string into a list."""
    if not raw:
        return []
    return [a.strip() for a in re.split(r"[,;]", raw) if a.strip()]
