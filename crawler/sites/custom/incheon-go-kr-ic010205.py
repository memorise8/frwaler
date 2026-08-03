# -*- coding: utf-8 -*-
"""인천광역시 보도자료 crawler (IC010205).

List:   GET https://www.incheon.go.kr/IC010205?curPage=N   (10 items/page, ~1605 pages)
Detail: GET https://www.incheon.go.kr/IC010205/view?repSeq=DOM_XXXX&curPage=N
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML parser with fallback chain
# ---------------------------------------------------------------------------

def _make_soup(html):
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class IncheonIC010205Crawler(BaseCrawler):
    """인천광역시 보도자료 (IC010205)."""

    site_id = "incheon-go-kr-ic010205"
    site_name = "Custom: incheon-go-kr-ic010205"
    base_url = "https://www.incheon.go.kr"

    _LIST_URL = "https://www.incheon.go.kr/IC010205"
    _DETAIL_BASE = "https://www.incheon.go.kr/IC010205/view"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))          # safety cap
    _MAX_WALL_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25 minutes

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """GET via curl; returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,*/*;q=0.8",
            "-H", "Accept-Language: ko-KR,ko;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=35)
                text = r.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as e:
                print(f"[{self.site_id}] curl error ({attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                wait = (1, 3, 9)[attempt]
                print(f"[{self.site_id}] retrying in {wait}s…")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _post_number(rep_seq):
        """DOM_0000000014695628 → '14695628'."""
        m = re.search(r'DOM_0*(\d+)', rep_seq)
        if m:
            return m.group(1)
        digits = re.sub(r'\D', '', rep_seq)
        return digits or None

    @staticmethod
    def _first_date(text):
        m = re.search(r'(\d{4}-\d{2}-\d{2})', text)
        return m.group(1) if m else ""

    def _parse_detail(self, soup, rep_seq, cur_page):
        """Return a paper dict from a parsed detail page."""
        detail_url = f"{self._DETAIL_BASE}?repSeq={rep_seq}&curPage={cur_page}"

        # Title
        title_tag = soup.select_one('.board-view-title')
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Published date — try meta section first, then JSON-LD
        published_date = ""
        listed_date = ""
        department = ""
        meta = soup.select_one('.board-view-meta')
        if meta:
            mt = meta.get_text(' ', strip=True)
            d = re.search(r'제공일시\s+(\d{4}-\d{2}-\d{2})', mt)
            if d:
                published_date = d.group(1)
                listed_date = published_date
            dept_m = re.search(r'담당부서\s+([\S]+)', mt)
            if dept_m:
                department = dept_m.group(1).rstrip('/')

        if not published_date:
            ld = soup.find('script', type='application/ld+json')
            if ld and ld.string:
                try:
                    published_date = json.loads(ld.string).get('datePublished', '')
                    listed_date = published_date
                except Exception:
                    pass

        # Abstract — full article body
        content_tag = (soup.select_one('.board-view-contents')
                       or soup.select_one('.cms_content'))
        abstract = ""
        if content_tag:
            for ph in content_tag.select('#hwpEditorBoardContent'):
                ph.decompose()
            abstract = re.sub(r'\s+', ' ', content_tag.get_text(' ', strip=True)).strip()

        # Attachments
        pdf_url = None
        original_filename = None
        attachments = []
        for a_tag in soup.select('a[href*="dmsFileDownload"]'):
            href = a_tag.get('href', '')
            full_url = self.base_url + href if href.startswith('/') else href
            fname_span = a_tag.find_previous('span', class_='file-name')
            fname = fname_span.get_text(strip=True) if fname_span else ""
            ext = fname.rsplit('.', 1)[-1].lower() if '.' in fname else ''
            attachments.append({'url': full_url, 'filename': fname})
            if ext == 'pdf' and not pdf_url:
                pdf_url = full_url
                original_filename = fname
        # Fall back to first attachment (often HWP)
        if not pdf_url and attachments:
            pdf_url = attachments[0]['url']
            original_filename = attachments[0]['filename']

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": rep_seq,
            "post_number": self._post_number(rep_seq),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": "",
            "publisher": "인천광역시",
            "department": department,
            "journal": "",
            "url": detail_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": "보도자료",
            "doi": "",
            "original_filename": original_filename,
            "metadata": json.dumps({
                "posted_date": listed_date,
                "originalFilename": original_filename,
                "repSeq": rep_seq,
                "department": department,
                "attachments": attachments,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 인천광역시 보도자료.

        Paginates through ?curPage=N, fetches detail page per item.
        """
        start_time = time.time()
        saved = 0
        page = 1
        seen_urls = set()
        lim_str = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > self._MAX_WALL_SEC:
                print(f"[{self.site_id}] Wall-clock budget exceeded. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            if page % 10 == 1 and page > 1:
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            # Fetch list page
            raw = self._curl_get(f"{self._LIST_URL}?curPage={page}")
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] List page {page} parse error: {exc}. Stopping.")
                break

            links = soup.select('a[href*="/IC010205/view"]')
            if not links:
                print(f"[{self.site_id}] No items at page {page}. Done.")
                break

            # Deduplicate
            new_links = []
            for a in links:
                href = a.get('href', '')
                if href and href not in seen_urls:
                    seen_urls.add(href)
                    new_links.append(a)

            if not new_links:
                print(f"[{self.site_id}] Page {page} all seen — pagination loop guard. Stopping.")
                break

            for a in new_links:
                if limit is not None and saved >= limit:
                    break

                href = a.get('href', '')
                m = re.search(r'repSeq=([^&]+)', href)
                if not m:
                    continue
                rep_seq = m.group(1)
                cp_m = re.search(r'curPage=(\d+)', href)
                cur_page = int(cp_m.group(1)) if cp_m else page

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(
                        f"{self._DETAIL_BASE}?repSeq={rep_seq}&curPage={cur_page}"
                    )
                    if not detail_raw:
                        print(f"[{self.site_id}] item {rep_seq} fetch failed. Skipping.")
                        continue

                    try:
                        detail_soup = _make_soup(detail_raw)
                    except Exception as pe:
                        print(f"[{self.site_id}] item {rep_seq} parse error: {pe}. Skipping.")
                        continue

                    paper = self._parse_detail(detail_soup, rep_seq, cur_page)

                    if not paper.get('title'):
                        print(f"[{self.site_id}] item {rep_seq} has no title. Skipping.")
                        continue

                    abstract = paper.get('abstract', '')
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {rep_seq} abstract too short "
                              f"({len(abstract)} chars). Skipping.")
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{lim_str}: {paper['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {rep_seq} failed: {exc}")
                    continue

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
