# -*- coding: utf-8 -*-
"""Crawler for NPS 공공데이터 개방 (nps.or.kr/inforls/publdata/).

List page: GET https://www.nps.or.kr/inforls/publdata/getOHAB0019M0List.do
  params: menuId, hmpgCd=01, hmpgBbsCd=BS20240191, sortSe=FR, pageIndex, searchText, searchGbu
  10 items per page, ~985 total items.

Detail page: GET https://www.nps.or.kr/inforls/publdata/getOHAB0019M1.do
  params: menuId, pstId, hmpgCd=01, hmpgBbsCd=BS20240191, sortSe=FR, pageIndex
"""

import json
import re
import subprocess
import time
from urllib.parse import urlencode, urljoin

from crawler.base_crawler import BaseCrawler

_BASE = "https://www.nps.or.kr"
_LIST_PATH = "/inforls/publdata/getOHAB0019M0List.do"
_DETAIL_PATH = "/inforls/publdata/getOHAB0019M1.do"
_MENU_ID = "MN24000873"
_HMPG_CD = "01"
_HMPG_BBS_CD = "BS20240191"
_PAGE_SIZE = 10
_MAX_PAGES = 200


def _try_soup(html):
    """Build BeautifulSoup with fallback parsers; return soup or None."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_text(soup_elem):
    """Extract clean text from a BeautifulSoup element."""
    if soup_elem is None:
        return ""
    text = soup_elem.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class NpsInforlsCrawler(BaseCrawler):
    """NPS 공공데이터 개방 crawler."""

    site_id = "nps-or-kr-inforls"
    site_name = "Custom: nps-or-kr-inforls"
    base_url = "https://www.nps.or.kr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, params=None):
        """GET via curl with TLS workaround and exponential-backoff retry.

        Returns decoded response text, or None after 3 failures.
        """
        if params:
            url = url + "?" + urlencode(params)
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8",
            "-H", "Referer: https://www.nps.or.kr/",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Empty response (attempt {attempt+1}), retry in {wait}s")
                time.sleep(wait)
            except Exception as exc:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] curl error (attempt {attempt+1}): {exc}, retry in {wait}s")
                if attempt < 2:
                    time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts for {url}")
        return None

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        params = {
            "menuId": _MENU_ID,
            "hmpgCd": _HMPG_CD,
            "hmpgBbsCd": _HMPG_BBS_CD,
            "sortSe": "FR",
            "pageIndex": str(page),
            "searchText": "",
            "searchGbu": "",
        }
        return self._curl_get(_BASE + _LIST_PATH, params)

    def _parse_list_page(self, html):
        """Return list of item dicts from a list-page HTML string.

        Each dict: title, pst_id, url, author, dept, phone, date, no.
        """
        try:
            soup = _try_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup parse error (list): {exc}")
            return []
        if soup is None:
            return []

        items = []
        # Find the data table — prefer the scoped div; fall back to full doc
        container = soup.find("div", class_="data-list") or soup
        for row in container.find_all("tr"):
            no_td = row.find("td", class_="no")
            title_td = row.find("td", class_="title")
            if not no_td or not title_td:
                continue
            link = title_td.find("a")
            if not link:
                continue

            href = link.get("href", "")
            title = link.get_text(strip=True)
            if not title or not href:
                continue

            pst_match = re.search(r"pstId=([^&]+)", href)
            if not pst_match:
                continue
            pst_id = pst_match.group(1)

            full_url = urljoin(_BASE, href)

            cells = row.find_all("td")
            # Expected order: no, title, file, author, dept, phone, date, views
            author = cells[3].get_text(strip=True) if len(cells) > 3 else ""
            dept = cells[4].get_text(strip=True) if len(cells) > 4 else ""
            phone = cells[5].get_text(strip=True) if len(cells) > 5 else ""
            date_raw = cells[6].get_text(strip=True) if len(cells) > 6 else ""

            items.append({
                "no": no_td.get_text(strip=True),
                "title": title,
                "pst_id": pst_id,
                "url": full_url,
                "author": author,
                "dept": dept,
                "phone": phone,
                "date": date_raw,
            })
        return items

    # ------------------------------------------------------------------
    # Detail parsing
    # ------------------------------------------------------------------

    def _fetch_detail(self, pst_id, page=1):
        params = {
            "menuId": _MENU_ID,
            "pstId": pst_id,
            "hmpgCd": _HMPG_CD,
            "hmpgBbsCd": _HMPG_BBS_CD,
            "sortSe": "FR",
            "pageIndex": str(page),
            "searchText": "",
            "searchGbu": "",
        }
        return self._curl_get(_BASE + _DETAIL_PATH, params)

    def _parse_detail(self, html):
        """Return (abstract, meta_dict) from a detail-page HTML string."""
        try:
            soup = _try_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup parse error (detail): {exc}")
            return "", {}
        if soup is None:
            return "", {}

        # Main content body
        content_div = soup.find("div", class_="view-contents-wrap")
        abstract = _strip_text(content_div)

        # Attached files
        files = []
        for file_item in soup.find_all("div", class_="file-item"):
            # File name — class may be "a-file zip", "a-file xls", etc.
            name_tag = file_item.find("p", class_=re.compile(r"a-file"))
            file_name = name_tag.get_text(strip=True) if name_tag else ""
            dl_link = file_item.find("a", class_="btn-file-down")
            file_href = dl_link.get("href", "") if dl_link else ""
            fl_match = re.search(r"fncAtchFileDownload\('([^']+)'", file_href)
            file_id = fl_match.group(1) if fl_match else ""
            if file_name or file_id:
                files.append({"name": file_name, "fileId": file_id})

        return abstract, {"attachedFiles": files}

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NPS 공공데이터 list and detail pages.

        Walks pages until (a) saved >= limit, (b) page returns 0 new records,
        or (c) safety cap of 200 pages reached. Wall-clock budget: 25 min.
        """
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        max_seconds = 25 * 60  # 25 minutes
        limit_str = str(limit) if limit is not None else "inf"

        for p in range(1, _MAX_PAGES + 1):
            # ── limit / time guards ──────────────────────────────────────
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached at page {p}, stopping.")
                break

            # ── progress log every 10 pages ─────────────────────────────
            if p == 1 or p % 10 == 0:
                print(f"[{self.site_id}] page {p}: saved {saved}/{limit_str}")

            # ── fetch list ──────────────────────────────────────────────
            raw_list = self._fetch_list_page(p)
            if not raw_list:
                print(f"[{self.site_id}] Failed to fetch list page {p}, stopping.")
                break

            items = self._parse_list_page(raw_list)
            if not items:
                print(f"[{self.site_id}] No items on page {p}, pagination complete.")
                break

            # ── URL deduplication — detect silent loop-back ───────────────
            new_items = []
            for item in items:
                if item["url"] in seen_urls:
                    # Already seen → paginator looped back to page 1
                    print(f"[{self.site_id}] Duplicate URL on page {p} (paginator loop), stopping.")
                    return saved
                new_items.append(item)

            if not new_items:
                print(f"[{self.site_id}] All items on page {p} already seen, stopping.")
                break

            # Mark all as seen before processing (avoids partial saves on error)
            for item in new_items:
                seen_urls.add(item["url"])

            # ── process each item ────────────────────────────────────────
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                pst_id = item["pst_id"]

                try:
                    time.sleep(self._delay)

                    detail_html = self._fetch_detail(pst_id, page=p)
                    if not detail_html:
                        print(f"[{self.site_id}] item {pst_id} failed: no detail HTML")
                        continue

                    abstract, detail_meta = self._parse_detail(detail_html)

                    # Skip items with very short abstracts
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {pst_id} abstract too short "
                            f"({len(abstract)} chars), skipping."
                        )
                        continue

                    # Normalise date 2026/04/30 → 2026-04-30
                    date_raw = item["date"].replace("/", "-").strip()

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": pst_id,
                        "title": item["title"],
                        "authors": json.dumps(
                            [item["author"]] if item["author"] else [],
                            ensure_ascii=False,
                        ),
                        "abstract": abstract,
                        "category": "",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": date_raw,
                        "url": item["url"],
                        "pdf_url": "",
                        "doi": "",
                        "department": item["dept"],
                        "metadata": json.dumps(
                            {
                                "no": item["no"],
                                "phone": item["phone"],
                                **detail_meta,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pst_id} failed: {exc}")
                    continue

        else:
            # for-loop exhausted without break → safety cap hit
            print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
