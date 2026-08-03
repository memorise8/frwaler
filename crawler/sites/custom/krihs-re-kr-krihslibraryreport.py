# -*- coding: utf-8 -*-
"""KRIHS 국토연구원 워킹페이퍼 crawler.

List page:  https://www.krihs.re.kr/krihsLibraryReport/briefList.es?mid=a10103090000&pub_kind=WKP
Detail page: https://library.krihs.re.kr/library/10120/contents/{content_id}  (Next.js SSR)

Strategy:
  1. Walk list pages (10 items/page, ~15 pages, ~149 items total).
  2. For each item, fetch the library detail page and extract the abstract
     from the rendered HTML <section><h5>소개</h5>…</section> block.
"""

import json
import math
import os
import re
import subprocess
import sys
import time
import os as _os

# Absolute import works when project root is on sys.path (test adds it).
_proj_root = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
if _proj_root not in sys.path:
    sys.path.insert(0, _proj_root)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_LIST_URL = "https://www.krihs.re.kr/krihsLibraryReport/briefList.es"
_LIST_MID = "a10103090000"
_LIST_PUB_KIND = "WKP"
_LIB_DETAIL_BASE = "https://library.krihs.re.kr/library/10120/contents"
_SITE_ID = "krihs-re-kr-krihslibraryreport"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class KRIHSLibraryReportCrawler(BaseCrawler):
    """Crawler for KRIHS 워킹페이퍼 (국토연구원 Working Papers)."""

    site_id = _SITE_ID
    site_name = "Custom: krihs-re-kr-krihslibraryreport"
    base_url = "https://www.krihs.re.kr"

    # ------------------------------------------------------------------ #
    # HTTP                                                                 #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url):
        """GET via curl with TLS workaround and 3-attempt exponential retry.

        Returns decoded text, or None on persistent failure.
        """
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30", "-L",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.7",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            url,
        ]
        delays = [1, 3, 9]
        for attempt, delay in enumerate(delays):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=40)
                if r.returncode == 0 and r.stdout:
                    return r.stdout.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[{_SITE_ID}] empty response (attempt {attempt+1}), retry in {delay}s")
                    time.sleep(delay)
            except Exception as exc:
                if attempt < 2:
                    print(f"[{_SITE_ID}] curl error (attempt {attempt+1}): {exc}, retry in {delay}s")
                    time.sleep(delay)
                else:
                    print(f"[{_SITE_ID}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # HTML parsing helpers                                                 #
    # ------------------------------------------------------------------ #

    def _make_soup(self, html):
        """BeautifulSoup with parser fallback chain."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ #
    # List page                                                            #
    # ------------------------------------------------------------------ #

    def _get_total_pages(self, soup):
        """Parse '페이지 1 / 15' from pagination area."""
        if not soup:
            return 15
        for text in soup.stripped_strings:
            m = re.search(r"페이지\s+\d+\s*/\s*(\d+)", text)
            if m:
                return int(m.group(1))
        # Fallback: compute from total item count "전체 149건"
        for text in soup.stripped_strings:
            m = re.search(r"전체\s+([\d,]+)", text)
            if m:
                n = int(m.group(1).replace(",", ""))
                return math.ceil(n / 10)
        return 15

    def _parse_list_page(self, html):
        """Return list of raw item dicts from one list page."""
        soup = self._make_soup(html)
        if not soup:
            return []

        blog_list = soup.find("ul", class_="blog_list")
        if not blog_list:
            return []

        items = []
        for li in blog_list.find_all("li"):
            try:
                li_str = str(li)

                # Native ID and numeric content_id from viewCntAdd(...)
                # Pattern: viewCntAdd('WKP','iw-7636384','/library/10120/contents/7636384')
                vc_m = re.search(
                    r"viewCntAdd\('([^']+)','(iw-(\d+))','/library/\d+/contents/(\d+)'",
                    li_str,
                )
                if not vc_m:
                    continue
                pub_kind = vc_m.group(1)     # "WKP"
                native_id = vc_m.group(2)    # "iw-7636384"
                content_id = vc_m.group(4)   # "7636384"

                # Title — remove icon <span> children first
                title = ""
                title_el = li.find("strong", class_="title")
                if title_el:
                    for span in title_el.find_all("span"):
                        span.decompose()
                    title = title_el.get_text(strip=True)

                # Authors — remove "저자" label, split by comma
                authors = ""
                author_el = li.find("p", class_="author")
                if author_el:
                    for lbl in author_el.find_all("strong", class_="label"):
                        lbl.decompose()
                    raw = author_el.get_text(strip=True)
                    authors = "; ".join(a.strip() for a in re.split(r"[,，]", raw) if a.strip())

                # Published date — find span.date with 발행일, normalise to YYYY-MM-DD
                pub_date = ""
                for span in li.find_all("span", class_="date"):
                    lbl = span.find("strong", class_="label")
                    if lbl and "발행일" in lbl.get_text():
                        lbl.decompose()
                        raw = span.get_text(strip=True)
                        dm = re.search(r"(\d{4})[-./년 ]+(\d{1,2})[-./월 ]+(\d{1,2})", raw)
                        if dm:
                            pub_date = (
                                f"{dm.group(1)}-{dm.group(2).zfill(2)}"
                                f"-{dm.group(3).zfill(2)}"
                            )
                        else:
                            dm2 = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
                            if dm2:
                                pub_date = dm2.group(1)
                        break

                # PDF download URL (direct link with accessType=down)
                pdf_url = None
                dl_link = li.find(
                    "a", href=re.compile(r"library\.krihs\.re\.kr.*accessType=down")
                )
                if dl_link:
                    pdf_url = dl_link["href"]

                items.append({
                    "native_id": native_id,
                    "content_id": content_id,
                    "pub_kind": pub_kind,
                    "title": title,
                    "authors": authors,
                    "pub_date": pub_date,
                    "pdf_url": pdf_url,
                    "detail_url": f"{_LIB_DETAIL_BASE}/{content_id}",
                })
            except Exception as exc:
                print(f"[{_SITE_ID}] list item parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------ #
    # Detail page — rendered HTML extraction                               #
    # ------------------------------------------------------------------ #

    def _parse_detail(self, html):
        """Extract abstract and metadata from the rendered detail page HTML."""
        result = {
            "abstract": "",
            "publisher": "국토연구원",
            "category": "",
            "original_filename": "",
            "report_number": "",
            "pages": "",
            "collection": "",
            "keywords": "",
            "_pmedia_id": "",
            "_pmedia_uuid": "",
            "_lmedia_id": "",
        }

        soup = self._make_soup(html)
        if not soup:
            return result

        # Metadata from dt/dd pairs (발행일, 카테고리, 글쓴이, 보고서번호, etc.)
        for dt in soup.find_all("dt"):
            try:
                dt_text = dt.get_text(strip=True)
                dd = dt.find_next_sibling("dd")
                if not dd:
                    continue
                dd_text = dd.get_text(strip=True)
                if "카테고리" in dt_text:
                    result["category"] = dd_text
                elif "글쓴이" in dt_text or "연구진" in dt_text:
                    result["publisher_authors"] = dd_text
                elif "발행일" in dt_text:
                    result["detail_pub_date"] = dd_text
                elif "보고서번호" in dt_text:
                    result["report_number"] = dd_text
                elif "페이지수" in dt_text:
                    result["pages"] = dd_text
                elif "과제구분" in dt_text or "collection" in dt_text.lower():
                    result["collection"] = dd_text
            except Exception:
                continue

        # Abstract: find <h5>소개</h5>, take the next sibling element
        for h in soup.find_all(["h5", "h4", "h3"]):
            try:
                if h.get_text(strip=True) != "소개":
                    continue
                sib = h.find_next_sibling()
                if sib:
                    text = sib.get_text(separator=" ", strip=True)
                    if len(text) >= 50:
                        result["abstract"] = text
                        break
                # Fallback: strip heading from parent
                parent = h.parent
                if parent:
                    h.decompose()
                    text = parent.get_text(separator=" ", strip=True)
                    if len(text) >= 50:
                        result["abstract"] = text
                break
            except Exception:
                continue

        # Original filename and media IDs from embedded RSC / JSON data
        fn_m = re.search(
            r'"pmediaId":(\d+)[^}]*?"pmediaUuid":"([^"]+)"[^}]*?"lmediaId":(\d+)'
            r'[^}]*?"title":"([^"]*\.(?:pdf|PDF))"',
            html,
            re.DOTALL,
        )
        if fn_m:
            result["original_filename"] = fn_m.group(4)
            result["_pmedia_id"] = fn_m.group(1)
            result["_pmedia_uuid"] = fn_m.group(2)
            result["_lmedia_id"] = fn_m.group(3)
        else:
            # Simpler fallback: just grab the first .pdf title
            fn_m2 = re.search(r'"title":"([^"]+\.(?:pdf|PDF))"', html)
            if fn_m2:
                result["original_filename"] = fn_m2.group(1)

        return result

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Walk list pages, fetch details, save records.

        Parameters
        ----------
        limit:
            Maximum number of items to save. None = unlimited.
        """
        saved = 0
        seen_urls: set = set()
        limit_str = str(limit) if limit is not None else "inf"
        start_ts = time.time()

        # ---- First page: also determines total page count ----
        first_html = self._curl_get(
            f"{_LIST_URL}?mid={_LIST_MID}&pub_kind={_LIST_PUB_KIND}&pageIndex=1"
        )
        if not first_html:
            print(f"[{_SITE_ID}] failed to fetch first list page")
            return 0

        soup0 = self._make_soup(first_html)
        total_pages = self._get_total_pages(soup0)
        cap = min(total_pages, _MAX_PAGES)
        print(f"[{_SITE_ID}] total pages: {total_pages} (cap: {cap})")
        if cap == _MAX_PAGES and total_pages > _MAX_PAGES:
            print(f"[{_SITE_ID}] safety cap of {_MAX_PAGES} pages applied")

        page = 1
        while page <= cap:
            # ---- Time budget guard ----
            if time.time() - start_ts > _MAX_WALL_SECS:
                print(f"[{_SITE_ID}] 25-min budget reached at page {page}, stopping cleanly")
                break

            # ---- Limit guard ----
            if limit is not None and saved >= limit:
                break

            # ---- Fetch list HTML ----
            if page == 1:
                html = first_html
            else:
                html = self._curl_get(
                    f"{_LIST_URL}?mid={_LIST_MID}&pub_kind={_LIST_PUB_KIND}&pageIndex={page}"
                )
                if not html:
                    print(f"[{_SITE_ID}] page {page}: fetch failed, skipping")
                    page += 1
                    continue

            # ---- Progress log every 10 pages ----
            if page == 1 or page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            # ---- Parse list items ----
            items = self._parse_list_page(html)
            if not items:
                print(f"[{_SITE_ID}] page {page}: no items found, stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                detail_url = item["detail_url"]
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    # ---- Fetch detail page ----
                    time.sleep(self._delay)
                    detail_html = self._curl_get(detail_url)
                    if not detail_html:
                        print(f"[{_SITE_ID}] detail fetch failed: {detail_url}")
                        continue

                    detail = self._parse_detail(detail_html)

                    # ---- Abstract quality gate ----
                    abstract = detail.get("abstract", "")
                    if len(abstract) < 50:
                        print(
                            f"[{_SITE_ID}] skipping {item['native_id']}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # ---- Build PDF URL from media IDs if RSC captured them ----
                    pdf_url = item.get("pdf_url")
                    if detail.get("_pmedia_id"):
                        pdf_url = (
                            "https://library.krihs.re.kr/library/api/media"
                            f"?pmediaId={detail['_pmedia_id']}"
                            f"&pmediaUuid={detail['_pmedia_uuid']}"
                            f"&lmediaId={detail['_lmedia_id']}"
                            f"&accessType=down"
                        )

                    # ---- Build metadata blob ----
                    meta = {
                        "posted_date": item["pub_date"],
                        "originalFilename": detail.get("original_filename", ""),
                        "reportNumber": detail.get("report_number", ""),
                        "pages": detail.get("pages", ""),
                        "collection": detail.get("collection", ""),
                        "pub_kind": item["pub_kind"],
                        "content_id": item["content_id"],
                        "native_id": item["native_id"],
                    }

                    # ---- Save ----
                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": item["native_id"],
                        "post_number": item["content_id"],
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": item["pub_date"],
                        "listed_date": item["pub_date"],
                        "authors": item["authors"],
                        "publisher": "국토연구원",
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "original_filename": detail.get("original_filename", ""),
                        "keywords": detail.get("keywords", ""),
                        "category": detail.get("category", ""),
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    })
                    saved += 1
                    print(f"[{_SITE_ID}] saved {saved}/{limit_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {item.get('native_id', '?')} failed: {exc}")
                    continue

            # ---- Deduplication stop: entire page already seen ----
            if new_on_page == 0 and page > 1:
                print(f"[{_SITE_ID}] page {page}: all items already seen, stopping")
                break

            page += 1
            time.sleep(0.3)  # brief inter-page courtesy delay

        print(f"[{_SITE_ID}] done: saved {saved} items")
        return saved
