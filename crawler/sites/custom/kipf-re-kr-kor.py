# -*- coding: utf-8 -*-
"""KIPF 센터 정기간행물 (한국조세재정연구원 — Center Periodicals) crawler.

List:   GET  https://www.kipf.re.kr/kor/Publication/CenterPeriodicals/kiPublish/CB/Center/list.do?pageIndex=N
Detail: POST https://www.kipf.re.kr/kor/Publication/CenterPeriodicals/kiPublish/CB/Center/view.do
             body: serialNo=<id>&pageIndex=<p>

Abstract is extracted from the "상세 내용" panel (.panel .cont) which contains
rich body text — consistently > 100 chars for this collection.
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.kipf.re.kr/kor/Publication/CenterPeriodicals/kiPublish/CB/Center/list.do"
_DETAIL_URL = "https://www.kipf.re.kr/kor/Publication/CenterPeriodicals/kiPublish/CB/Center/view.do"
_BASE = "https://www.kipf.re.kr"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes


class KipfReKrKorCrawler(BaseCrawler):
    """KIPF 한국조세재정연구원 정책보고서 (CD4) crawler."""

    site_id = "kipf-re-kr-kor"
    site_name = "Custom: kipf-re-kr-kor"
    base_url = "https://www.kipf.re.kr"

    # ------------------------------------------------------------------
    # curl fetch
    # ------------------------------------------------------------------

    def _curl_fetch(self, url, method="GET", data=None, retries=3):
        """Fetch URL via curl with TLS workaround + exponential backoff.

        Returns decoded text on success, None after all retries exhausted.
        """
        backoffs = [1, 3, 9]
        for attempt in range(retries):
            try:
                cmd = [
                    "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                    "-H", f"User-Agent: {self.USER_AGENT}",
                    "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8",
                    "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                ]
                if method.upper() == "POST" and data:
                    cmd += [
                        "-X", "POST",
                        "-H", "Content-Type: application/x-www-form-urlencoded",
                    ]
                    for k, v in data.items():
                        cmd += ["-d", f"{k}={v}"]
                cmd.append(url)

                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if not raw:
                    raise ValueError("empty response body")
                try:
                    return raw.decode("utf-8")
                except UnicodeDecodeError:
                    return raw.decode("utf-8", errors="replace")

            except Exception as exc:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                print(f"[{self.site_id}] curl attempt {attempt + 1}/{retries} failed: {exc}")
                if attempt < retries - 1:
                    time.sleep(wait)

        return None

    # ------------------------------------------------------------------
    # HTML helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html):
        """BeautifulSoup with fallback parser chain. Returns None on all failures."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _strip_html(html_str):
        """Strip HTML tags and collapse whitespace."""
        text = re.sub(r"<[^>]+>", " ", html_str or "")
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list(self, html):
        """Return list of {serial_no, title, authors, date, keywords} dicts."""
        items = []
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] list soup construction failed: {exc}")
            return items
        if not soup:
            return items

        for a_tag in soup.find_all("a", onclick=True):
            onclick = a_tag.get("onclick", "")
            m = re.search(r"fn_search_detail\('(\d+)'\)", onclick)
            if not m:
                continue
            serial_no = m.group(1)

            try:
                tit = a_tag.find("strong", class_="tit")
                title = tit.get_text(strip=True) if tit else ""

                authors = []
                date = ""
                keywords = []

                for li in a_tag.find_all("li"):
                    strong = li.find("strong")
                    if not strong:
                        continue
                    label = strong.get_text(strip=True)
                    span = li.find("span")
                    val = span.get_text(strip=True) if span else ""
                    if "저자" in label and val:
                        authors = [a.strip() for a in val.split(",") if a.strip()]
                    elif "발간월" in label and val:
                        date = val

                tag_div = a_tag.find("div", class_="tag")
                if tag_div:
                    keywords = [
                        em.get_text(strip=True)
                        for em in tag_div.find_all("em")
                        if em.get_text(strip=True)
                    ]

                items.append({
                    "serial_no": serial_no,
                    "title": title,
                    "authors": authors,
                    "date": date,
                    "keywords": keywords,
                })

            except Exception as exc:
                print(f"[{self.site_id}] list item parse error (serialNo={serial_no}): {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html, serial_no):
        """Return {title, authors, date, keywords, abstract, pdf_url} from detail page."""
        result = {
            "title": "", "authors": [], "date": "",
            "keywords": [], "abstract": "", "pdf_url": "",
        }
        try:
            soup = self._make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] detail soup construction failed (serialNo={serial_no}): {exc}")
            return result
        if not soup:
            return result

        # Title
        title_tag = soup.find("strong", class_="title")
        if title_tag:
            result["title"] = title_tag.get_text(strip=True)

        # Keywords from .hashtag
        hashtag = soup.find("div", class_="hashtag")
        if hashtag:
            result["keywords"] = [
                s.get_text(strip=True)
                for s in hashtag.find_all("span")
                if s.get_text(strip=True)
            ]

        # Date + authors from .detail-info
        info_div = soup.find("div", class_="detail-info")
        if info_div:
            for li in info_div.find_all("li"):
                strong = li.find("strong")
                if not strong:
                    continue
                label = strong.get_text(strip=True)
                if "발행월" in label or "발간월" in label:
                    span = li.find("span")
                    if span:
                        result["date"] = span.get_text(strip=True)
                elif "저" in label:
                    spans = li.find_all("span")
                    names = [s.get_text(strip=True) for s in spans if s.get_text(strip=True)]
                    if names:
                        result["authors"] = names

        # Abstract from 상세 내용 panel
        abstract_cont = None
        for panel in soup.find_all("div", class_="panel"):
            h2 = panel.find("h2")
            if h2 and "상세 내용" in h2.get_text():
                abstract_cont = panel.find("div", class_="cont")
                break
        if not abstract_cont:
            article = soup.find("div", class_="article")
            if article:
                abstract_cont = article.find("div", class_="cont")
        if not abstract_cont:
            abstract_cont = soup.find("div", class_="cont")
        if abstract_cont:
            result["abstract"] = self._strip_html(str(abstract_cont))

        # PDF URL — first href containing atchFileId (prefer FileDown.do)
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "atchFileId=" in href:
                m_id = re.search(r"atchFileId=([A-Za-z0-9_]+)", href)
                m_sn = re.search(r"fileSn=(\d+)", href)
                if m_id:
                    atch_id = m_id.group(1)
                    file_sn = m_sn.group(1) if m_sn else "0"
                    result["pdf_url"] = (
                        f"{_BASE}/cmm/fms/FileDown.do"
                        f"?atchFileId={atch_id}&fileSn={file_sn}"
                    )
                    break

        # Fallback: extract atchFileId from raw HTML (covers onclick-only links)
        if not result["pdf_url"]:
            m_id = re.search(r"atchFileId=([A-Za-z0-9_]+)", html)
            if m_id:
                result["pdf_url"] = (
                    f"{_BASE}/cmm/fms/FileDown.do"
                    f"?atchFileId={m_id.group(1)}&fileSn=0"
                )

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl KIPF 정책보고서 (CD4 category).

        Paginates list.do up to _MAX_PAGES, fetches each item's detail page
        via POST, and saves via _save_paper. Stops when limit is reached,
        pages are exhausted, or the wall-clock budget is exceeded.
        """
        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):

            # Wall clock guard
            if time.time() - start_time > _MAX_WALL_SECONDS:
                print(f"[{self.site_id}] Wall clock budget ({_MAX_WALL_SECONDS}s) exceeded "
                      f"at page {page}. Exiting cleanly.")
                break

            if limit is not None and saved >= limit:
                break

            if page == _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            # Fetch list page (GET with pageIndex query param)
            html = self._curl_fetch(f"{_LIST_URL}?pageIndex={page}")
            if not html:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            items = self._parse_list(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                serial_no = item["serial_no"]
                item_url = (
                    f"{_BASE}/kor/Publication/CenterPeriodicals/kiPublish/CB/Center"
                    f"/view.do?serialNo={serial_no}"
                )

                # URL deduplication — prevents infinite loop if server recycles pages
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)

                    detail_html = self._curl_fetch(
                        _DETAIL_URL, method="POST",
                        data={"serialNo": serial_no, "pageIndex": str(page)},
                    )
                    if not detail_html:
                        print(f"[{self.site_id}] item {serial_no}: detail fetch failed, skipping")
                        continue

                    detail = self._parse_detail(detail_html, serial_no)

                    title = detail["title"] or item["title"]
                    if not title:
                        print(f"[{self.site_id}] item {serial_no}: no title, skipping")
                        continue

                    authors = detail["authors"] or item["authors"]
                    date = detail["date"] or item["date"]
                    keywords = detail["keywords"] or item["keywords"]
                    abstract = detail["abstract"]
                    pdf_url = detail["pdf_url"]

                    # Normalize month-only date "2026-03" → "2026-03-01"
                    if date and re.match(r"^\d{4}-\d{2}$", date.strip()):
                        date = date.strip() + "-01"

                    # Skip items with insufficient abstract (test requires >= 100 chars)
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] item {serial_no}: "
                            f"abstract too short ({len(abstract)} chars), skipping"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": serial_no,
                        "title": title,
                        "authors": json.dumps(authors, ensure_ascii=False),
                        "abstract": abstract,
                        "category": "정기간행물",
                        "keywords": json.dumps(keywords, ensure_ascii=False),
                        "published_date": date,
                        "url": item_url,
                        "pdf_url": pdf_url,
                        "doi": "",
                        "department": "한국조세재정연구원",
                        "metadata": json.dumps(
                            {"serialNo": serial_no, "category": "CB"},
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_or_inf}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {serial_no} failed: {exc}")
                    continue

            # No new URLs → paginator looped back or past end
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page} (all already seen). Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
