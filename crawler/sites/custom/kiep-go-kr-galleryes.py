# -*- coding: utf-8 -*-
"""KIEP (Korea Institute for International Economic Policy) publications crawler.

Starting URL:
    https://www.kiep.go.kr/gallery.es?mid=a10101010000&bid=0001&cg_code=C03%2CC05%2CC02%2CC13%2CC01%2CC19%2CC17%2CC11%2CC20

The site is an "es" (Xpress-Engine-like) gallery board. List pages are paged via
``nPage``; each item has a numeric ``list_no`` that is both its native ID and a
stable sort key for incremental collection. Detail pages expose the full
Korean abstract (국문요약), an English abstract fallback (영문요약), authors,
publish date, category tags, and a single attachment download link
(``galleryDownload.es``).
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class KiepGoKrGalleryesCrawler(BaseCrawler):
    """Crawler for KIEP (kiep.go.kr) gallery.es publication board."""

    site_id = "kiep-go-kr-galleryes"
    site_name = "Custom: kiep-go-kr-galleryes"
    base_url = "https://www.kiep.go.kr"

    _MID = "a10101010000"
    _BID = "0001"
    _CG_CODE = "C03,C05,C02,C13,C01,C19,C17,C11,C20"
    _LIST_URL = "https://www.kiep.go.kr/gallery.es"
    _DETAIL_URL = "https://www.kiep.go.kr/gallery.es"
    _DOWNLOAD_URL = "https://www.kiep.go.kr/galleryDownload.es"
    _PUBLISHER = "대외경제정책연구원"

    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: str = "") -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        full_url = f"{url}?{params}" if params else url
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            full_url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {full_url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_head_filename(self, url: str) -> str | None:
        """HEAD via curl to extract Content-Disposition filename. Best-effort, no retries."""
        cmd = [
            "curl", "-skI", "--tls-max", "1.3", "--max-time", "20",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=25)
            text = result.stdout.decode("utf-8", errors="replace")
        except Exception:
            return None
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";\r\n]+)"?', text, re.IGNORECASE)
        if not m:
            return None
        name = m.group(1).strip()
        return name or None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _list_page_ids(self, page: int) -> list:
        """Return ordered, de-duplicated list_no values found on a list page."""
        params = (
            f"mid={self._MID}&bid={self._BID}"
            f"&cg_code={self._CG_CODE.replace(',', '%2C')}&nPage={page}"
        )
        raw = self._curl_get(self._LIST_URL, params)
        if not raw:
            return []
        ids = []
        seen = set()
        for m in re.finditer(r"goView\('(\d+)'\)", raw):
            list_no = m.group(1)
            if list_no not in seen:
                seen.add(list_no)
                ids.append(list_no)
        return ids

    def _parse_detail(self, raw_html: str, list_no: str) -> dict | None:
        """Parse a detail page into a flat dict, or None if unparseable."""
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for list_no={list_no}: {exc}")
            return None

        view = soup.select_one("div.board_view")
        if not view:
            return None

        title_tag = view.select_one("strong.title")
        doc_type = None
        title = ""
        if title_tag:
            span = title_tag.find("span")
            if span:
                doc_type = span.get_text(strip=True)
                span.extract()
            title = title_tag.get_text(" ", strip=True)
        if not title:
            return None

        info = view.select_one("p.info")
        authors = []
        issue_no = None
        language = None
        published_raw = None
        if info:
            author_span = info.find("strong", string="저자")
            if author_span:
                parent_span = author_span.find_parent("span")
                if parent_span:
                    authors = [a.get_text(strip=True) for a in parent_span.find_all("a") if a.get_text(strip=True)]
            for span in info.find_all("span"):
                strong = span.find("strong")
                if not strong:
                    continue
                label = strong.get_text(strip=True)
                value = span.get_text(" ", strip=True).replace(label, "", 1).strip()
                if label == "발간번호":
                    issue_no = value
                elif label == "자료언어":
                    language = value
                elif label == "발간일":
                    published_raw = value

        category_tag = view.select_one("span.category")
        categories = []
        if category_tag:
            categories = [a.get_text(strip=True) for a in category_tag.find_all("a") if a.get_text(strip=True)]

        # Abstract: prefer 국문요약, fall back to 영문요약, fall back to first item.
        abstract = ""
        preferred_order = ["국문요약", "영문요약"]
        items = view.select("div.cont div.item")
        by_label = {}
        for item in items:
            btn = item.find("button")
            txt = item.find("div", class_="txt")
            if not btn or not txt:
                continue
            by_label[btn.get_text(strip=True)] = txt.get_text("\n", strip=True)
        for label in preferred_order:
            if label in by_label and by_label[label].strip():
                abstract = by_label[label].strip()
                break
        if not abstract and by_label:
            abstract = next(iter(by_label.values())).strip()
        abstract = re.sub(r"\n{2,}", "\n\n", abstract).strip()

        pdf_url = None
        dl_link = view.select_one('a[href*="galleryDownload.es"]')
        if dl_link and dl_link.get("href"):
            href = dl_link["href"]
            if href.startswith("http"):
                pdf_url = href
            else:
                pdf_url = self.base_url + ("" if href.startswith("/") else "/") + href

        published_date = None
        if published_raw:
            m = re.match(r"(\d{4})[.\-](\d{1,2})[.\-](\d{1,2})", published_raw)
            if m:
                published_date = f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"

        return {
            "list_no": list_no,
            "doc_type": doc_type,
            "title": title,
            "authors": authors,
            "issue_no": issue_no,
            "language": language,
            "published_raw": published_raw,
            "published_date": published_date,
            "categories": categories,
            "abstract": abstract,
            "pdf_url": pdf_url,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl KIEP publications, walking list pages until limit/exhaustion/cap."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 0
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                page += 1
                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                ids = self._list_page_ids(page)
                if not ids:
                    print(f"[{self.site_id}] page {page}: 0 records. Stopping.")
                    break

                new_ids = [i for i in ids if i not in seen_urls]
                if not new_ids:
                    print(f"[{self.site_id}] page {page}: all {len(ids)} records already seen. Stopping.")
                    break

                for list_no in new_ids:
                    if limit is not None and saved >= limit:
                        break
                    seen_urls.add(list_no)

                    try:
                        detail_url = (
                            f"{self._DETAIL_URL}?mid={self._MID}&bid={self._BID}"
                            f"&act=view&list_no={list_no}"
                        )
                        raw = self._curl_get(
                            self._DETAIL_URL,
                            f"mid={self._MID}&bid={self._BID}&act=view&list_no={list_no}",
                        )
                        if not raw:
                            print(f"[{self.site_id}] item {list_no}: failed to fetch detail page. Skipping.")
                            continue

                        parsed = self._parse_detail(raw, list_no)
                        if not parsed:
                            print(f"[{self.site_id}] item {list_no}: could not parse detail page. Skipping.")
                            continue

                        if len(parsed["abstract"]) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] item {list_no}: abstract too short "
                                f"({len(parsed['abstract'])} chars). Skipping."
                            )
                            continue

                        original_filename = None
                        if parsed["pdf_url"]:
                            original_filename = self._curl_head_filename(parsed["pdf_url"])

                        listed_date = parsed["published_date"]
                        category_str = ", ".join(parsed["categories"]) if parsed["categories"] else None
                        keywords_str = ",".join(parsed["categories"]) if parsed["categories"] else None

                        meta = {
                            "posted_date": parsed["published_raw"],
                            "list_no": list_no,
                            "issue_no": parsed["issue_no"],
                            "language": parsed["language"],
                            "doc_type": parsed["doc_type"],
                        }
                        if original_filename:
                            meta["originalFilename"] = original_filename

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": str(list_no),
                            "post_number": str(list_no),
                            "title": parsed["title"],
                            "abstract": parsed["abstract"],
                            "published_date": parsed["published_date"],
                            "listed_date": listed_date,
                            "authors": "; ".join(parsed["authors"]) if parsed["authors"] else None,
                            "publisher": self._PUBLISHER,
                            "department": None,
                            "journal": None,
                            "url": detail_url,
                            "pdf_url": parsed["pdf_url"],
                            "keywords": keywords_str,
                            "category": category_str,
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {parsed['title'][:60]}")

                        time.sleep(self._delay)

                    except Exception as exc:
                        print(f"[{self.site_id}] item {list_no} failed: {exc}; continuing.")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
