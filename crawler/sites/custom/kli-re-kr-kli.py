# -*- coding: utf-8 -*-
"""KLI 한국노동연구원 (Korea Labor Institute) 연구보고서 crawler.

Target: https://www.kli.re.kr/kli/rschRptpList.es?mid=a10102060000

List  : GET /kli/rschRptpList.es?mid=a10102060000&nPage={n}
Detail: GET /kli/rschRptpView.es?pblct_sn={id}&mid=a10102060000&nPage=1&sch_yr=&sch_type=&sch_keyword=&sch_rsch_fld_no=
PDF   : GET /kliFileDownload?fileName=...&fileNameOrg=...&filePath1=...
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.kli.re.kr"
_MID = "a10102060000"
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


def _abs_url(href: str) -> str:
    """Build an absolute, percent-encoded URL from a raw (possibly
    Korean/space-containing) href taken straight out of the HTML."""
    if not href:
        return ""
    if href.startswith("http://") or href.startswith("https://"):
        base = href
    else:
        base = _BASE + (href if href.startswith("/") else "/" + href)
    return urllib.parse.quote(base, safe="/?&=:%")


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

class KliReKrKliCrawler(BaseCrawler):
    """Crawler for KLI 한국노동연구원 연구보고서 (mid=a10102060000)."""

    site_id = "kli-re-kr-kli"
    site_name = "Custom: kli-re-kr-kli"
    base_url = _BASE

    _MAX_PAGES = 200
    _TIMEOUT_SECS = 25 * 60   # 25 minutes
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

            list_url = f"{_BASE}/kli/rschRptpList.es?mid={_MID}&nPage={page}"
            list_html = _curl_get(list_url, referer=self.base_url)
            if not list_html:
                print(f"[{self.site_id}] List page {page} failed after retries. Stopping.")
                break

            try:
                soup = _bs4(list_html)
            except Exception as exc:
                print(f"[{self.site_id}] Parse error list page {page}: {exc}. Skipping.")
                continue

            table = soup.find("table", class_="tstyle_list")
            if not table:
                print(f"[{self.site_id}] No result table on page {page}. Done.")
                break

            tbody = table.find("tbody")
            rows = tbody.find_all("tr") if tbody else []
            if not rows:
                print(f"[{self.site_id}] No rows on page {page}. Done.")
                break

            new_on_page = 0

            for tr in rows:
                if limit is not None and saved >= limit:
                    break

                pblct_sn = "?"
                try:
                    title_td = tr.find("td", attrs={"aria-label": "제목"})
                    if not title_td:
                        continue
                    anchor = title_td.find("a")
                    if not anchor:
                        continue

                    href = anchor.get("href", "")
                    m = re.search(r"pblct_sn=(\d+)", href)
                    if not m:
                        continue
                    pblct_sn = m.group(1)

                    detail_url = (
                        f"{_BASE}/kli/rschRptpView.es?pblct_sn={pblct_sn}&mid={_MID}"
                        f"&nPage=1&sch_yr=&sch_type=&sch_keyword=&sch_rsch_fld_no="
                    )

                    # URL dedup — detects paginator loop-back
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    # --- list-level fields ---
                    title = _text(anchor)

                    num_td = tr.find("td", attrs={"aria-label": "번호"})
                    list_no = _text(num_td) or ""

                    cate_td = tr.find("td", attrs={"aria-label": "구분"})
                    category = _text(cate_td)

                    auth_td = tr.find("td", attrs={"aria-label": "저자"})
                    authors_raw = _text(auth_td)
                    authors = "; ".join(
                        a.strip() for a in re.split(r"[,，]", authors_raw) if a.strip()
                    )

                    date_td = tr.find("td", attrs={"aria-label": "출판일"})
                    raw_date = _text(date_td)
                    published_date = _norm_date(raw_date)

                    # --- PDF: prefer 첨부파일 (download link with original filename) ---
                    pdf_url = ""
                    original_filename = None
                    attach_td = tr.find("td", attrs={"aria-label": "첨부파일"})
                    attach_a = attach_td.find("a", href=re.compile(r"kliFileDownload")) if attach_td else None
                    if attach_a:
                        raw_href = attach_a.get("href", "")
                        pdf_url = _abs_url(raw_href)
                        fn_m = re.search(r"fileNameOrg=([^&]+)", raw_href)
                        if fn_m:
                            original_filename = fn_m.group(1).strip()

                    if not pdf_url:
                        orig_td = tr.find("td", attrs={"aria-label": "원문"})
                        orig_a = orig_td.find("a", href=re.compile(r"pdfPreviewDownload")) if orig_td else None
                        if orig_a:
                            pdf_url = _abs_url(orig_a.get("href", ""))

                    # --- detail page ---
                    time.sleep(self._delay)
                    detail_html = _curl_get(detail_url, referer=list_url)

                    abstract = ""
                    toc = ""
                    isbn = ""
                    page_count = ""

                    if detail_html:
                        try:
                            dsoup = _bs4(detail_html)

                            group2 = dsoup.find("div", class_="group2")
                            if group2:
                                cont = group2.find("div", class_="cont")
                                toc = _text(cont)

                            # Richer metadata / authors from info_wrap list
                            info_wrap = dsoup.find("ul", class_="info_wrap")
                            if info_wrap:
                                for li in info_wrap.find_all("li", recursive=False):
                                    em = li.find("em")
                                    if not em:
                                        continue
                                    key = _text(em)
                                    span = li.find("span")
                                    val = _text(span)
                                    if key == "저자" and val:
                                        authors = "; ".join(
                                            a.strip() for a in re.split(r"[,，]", val) if a.strip()
                                        )
                                    elif key == "출판일" and val:
                                        d = _norm_date(val)
                                        if d:
                                            published_date = d
                                    elif key == "ISBN" and val:
                                        isbn = val
                                    elif key == "페이지 수" and val:
                                        page_count = val

                            # PDF from detail download button (preferred over list)
                            btn_wrap = dsoup.find("div", class_="btn_wrap")
                            dl_btn = btn_wrap.find("a", href=re.compile(r"kliFileDownload")) if btn_wrap else None
                            if dl_btn:
                                dh = dl_btn.get("href", "")
                                if dh:
                                    pdf_url = _abs_url(dh)
                                    fn_m = re.search(r"fileNameOrg=([^&]+)", dh)
                                    if fn_m:
                                        original_filename = fn_m.group(1).strip()

                        except Exception as exc:
                            print(f"[{self.site_id}] detail parse error {pblct_sn}: {exc}")

                    if toc:
                        abstract = "[목차] " + toc

                    # Skip if abstract too short
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Skipping {pblct_sn}: "
                            f"abstract too short ({len(abstract)} chars): {title[:50]}"
                        )
                        continue

                    metadata = {
                        "posted_date": raw_date,
                        "originalFilename": original_filename or "",
                        "pblct_sn": pblct_sn,
                        "list_no": list_no,
                        "isbn": isbn,
                        "page_count": page_count,
                    }

                    paper = {
                        "site_id": self.site_id,
                        "external_id": pblct_sn,
                        "title": title,
                        "authors": authors,
                        "abstract": abstract,
                        "category": category,
                        "keywords": "",
                        "published_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url or None,
                        "doi": "",
                        "department": "한국노동연구원",
                        "publisher": "한국노동연구원",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pblct_sn} failed: {exc}; continuing")
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
    m = re.search(r"(\d{4})[./\-](\d{1,2})[./\-](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return ""
