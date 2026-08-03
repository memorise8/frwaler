# -*- coding: utf-8 -*-
"""NRC 경제인문사회연구회 보도자료 crawler (nrc-re-kr-boardes).

Board URL: https://www.nrc.re.kr/board.es?mid=a12102000000&bid=0015
Structure: HTML table listing with per-item detail pages.
Content: Press releases; text in <div class="tb_contents">; images-only posts skipped.
"""

import json
import os
import re
import subprocess
import sys
import time

# Ensure project root on sys.path when loaded via spec_from_file_location
_HERE = os.path.abspath(os.path.dirname(__file__))
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from crawler.base_crawler import BaseCrawler


class NRCBoardesCrawler(BaseCrawler):
    """Crawler for NRC 경제인문사회연구회 보도자료."""

    site_id = "nrc-re-kr-boardes"
    site_name = "Custom: nrc-re-kr-boardes"
    base_url = "https://www.nrc.re.kr"

    _LIST_BASE = "https://www.nrc.re.kr/board.es"
    _MID = "a12102000000"
    _BID = "0015"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT = 100  # skip items with fewer chars of extracted text

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, max_tries=3):
        """Fetch URL via curl with TLS workaround and exponential backoff."""
        for attempt in range(max_tries):
            try:
                result = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                    capture_output=True,
                    timeout=35,
                )
                raw = result.stdout
                if raw and raw.strip():
                    try:
                        return raw.decode("utf-8")
                    except UnicodeDecodeError:
                        return raw.decode("utf-8", errors="replace")
                if attempt < max_tries - 1:
                    wait = 3 ** attempt  # 1s, 3s
                    print(f"[{self.site_id}] Empty response (attempt {attempt + 1}), "
                          f"retrying in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/{max_tries}): {exc}")
                if attempt < max_tries - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _make_soup(html):
        """BeautifulSoup with parser fallback: html5lib → lxml → html.parser."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return BeautifulSoup(html, "html.parser")

    @staticmethod
    def _clean_text(raw):
        raw = re.sub(r"&[a-z]+;", " ", raw)
        raw = re.sub(r"\s+", " ", raw)
        return raw.strip()

    def _parse_list_page(self, html):
        """Return list of (list_no, title, date) from a board listing page."""
        items = []
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] Failed to parse list HTML: {exc}")
            return items

        try:
            table = soup.find("table", class_="tstyle_list")
            if not table:
                return items
            for row in table.find_all("tr"):
                td_title = row.find("td", class_="title")
                if not td_title:
                    continue
                a_tag = td_title.find("a")
                if not a_tag:
                    continue
                href = a_tag.get("href", "")
                m = re.search(r"list_no=(\d+)", href)
                if not m:
                    continue
                list_no = m.group(1)
                # Remove badge spans/icons before extracting title text
                for junk in a_tag.find_all(["span", "i"]):
                    junk.decompose()
                title = a_tag.get_text(strip=True)
                # Date column
                date = ""
                for td in row.find_all("td"):
                    txt = td.get_text(strip=True)
                    if re.match(r"\d{4}-\d{2}-\d{2}", txt):
                        date = txt[:10]
                        break
                items.append((list_no, title, date))
        except Exception as exc:
            print(f"[{self.site_id}] Error walking list rows: {exc}")
        return items

    def _parse_detail(self, html, list_no):
        """Return (title, abstract, date, pdf_url) from a detail page."""
        title = abstract = date = pdf_url = ""
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] Failed to parse detail HTML for {list_no}: {exc}")
            return title, abstract, date, pdf_url

        try:
            view = soup.find("div", class_="tstyle_view")
            if not view:
                return title, abstract, date, pdf_url

            # Title
            title_el = view.find("div", class_="title")
            if title_el:
                title = title_el.get_text(strip=True)

            # Date (작성일시)
            date_li = view.find("li", class_="date")
            if date_li:
                span = date_li.find("span")
                if span:
                    m = re.search(r"\d{4}-\d{2}-\d{2}", span.get_text())
                    if m:
                        date = m.group(0)

            # Main text content (tb_contents)
            content_div = view.find("div", class_="tb_contents")
            if content_div:
                raw_text = content_div.get_text(separator=" ")
                abstract = self._clean_text(raw_text)

            # PDF attachment link
            file_div = view.find("div", class_="add_file_list")
            if file_div:
                for a in file_div.find_all("a", class_="file-down"):
                    href = a.get("href", "")
                    label = a.get("title", "") + href
                    if ".pdf" in label.lower():
                        pdf_url = (self.base_url + href
                                   if href.startswith("/") else href)
                        break
                # Fallback: first download link regardless of type
                if not pdf_url:
                    first = file_div.find("a", class_="file-down")
                    if first:
                        href = first.get("href", "")
                        if href:
                            pdf_url = (self.base_url + href
                                       if href.startswith("/") else href)
        except Exception as exc:
            print(f"[{self.site_id}] Error extracting detail fields for {list_no}: {exc}")

        return title, abstract, date, pdf_url

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Walk board pages and save press-release items with substantive text.

        Skips items whose extracted abstract is shorter than _MIN_ABSTRACT chars
        (image-only posts). Stops after `limit` saved items, 200 pages, or
        25 minutes of wall time.
        """
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "∞"
        start_time = time.time()

        for page in range(1, self._MAX_PAGES + 1):
            # Wall-clock safety
            if time.time() - start_time > self._MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall-clock limit reached at page {page}. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Exiting.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            list_url = (f"{self._LIST_BASE}?mid={self._MID}"
                        f"&bid={self._BID}&nPage={page}")
            raw_list = self._curl_get(list_url)
            if not raw_list:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list_page(raw_list)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_on_page = 0
            for list_no, list_title, list_date in items:
                if limit is not None and saved >= limit:
                    break

                detail_url = (
                    f"{self._LIST_BASE}?mid={self._MID}&bid={self._BID}"
                    f"&act=view&list_no={list_no}&tag=&nPage={page}"
                )

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    raw_detail = self._curl_get(detail_url)
                    if not raw_detail:
                        print(f"[{self.site_id}] item {list_no} failed: empty detail response")
                        continue

                    title, abstract, date, pdf_url = self._parse_detail(raw_detail, list_no)

                    # Fallback to list-page values
                    if not title:
                        title = list_title
                    if not date:
                        date = list_date

                    if len(abstract) < self._MIN_ABSTRACT:
                        print(f"[{self.site_id}] item {list_no} short abstract "
                              f"({len(abstract)} chars): {list_title[:50]}. Skipping.")
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": list_no,
                        "title": title,
                        "authors": json.dumps([], ensure_ascii=False),
                        "abstract": abstract,
                        "category": "보도자료",
                        "keywords": json.dumps([], ensure_ascii=False),
                        "published_date": date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "경제인문사회연구회",
                        "metadata": json.dumps({
                            "board_id": self._BID,
                            "mid": self._MID,
                            "list_no": list_no,
                            "page": page,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {list_no} failed: {exc}")
                    continue

            # Detect pagination loop-back (all items on this page already seen)
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page}. Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
