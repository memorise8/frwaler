# -*- coding: utf-8 -*-
"""Gangwon Research Institute report crawler.

Starting URL:
    https://www.gi.re.kr/Home/H10000/H10100/pmsReportList

The site renders report lists and details as HTML. Pagination uses the
``page`` query parameter and details use ``pmsReportView?projcd=...``.
PDF attachments, when present, are exposed as anchors on the detail page.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler


_LIST_URL = "https://www.gi.re.kr/Home/H10000/H10100/pmsReportList"
_DETAIL_URL = "https://www.gi.re.kr/Home/H10000/H10100/pmsReportView"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_MIN_ABSTRACT_CHARS = 100


class GiReKrHomeCrawler(BaseCrawler):
    """Crawler for 강원연구원 연구보고서."""

    site_id = "gi-re-kr-home"
    site_name = "Custom: gi-re-kr-home"
    base_url = "https://www.gi.re.kr"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, params=None, retries=3):
        """Fetch a URL using curl with Korean-site TLS workarounds.

        Returns decoded text, using replacement characters for mixed/bad
        encodings. Retries use 1s, 3s, and 9s exponential backoff.
        """
        if params:
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{urllib.parse.urlencode(params)}"

        backoffs = [1, 3, 9]
        for attempt in range(retries):
            try:
                cmd = [
                    "curl",
                    "--tls-max",
                    "1.3",
                    "-sk",
                    "-L",
                    "--max-time",
                    "45",
                    "-H",
                    f"User-Agent: {self.USER_AGENT}",
                    "-H",
                    "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "-H",
                    "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                    "-H",
                    f"Referer: {self.base_url}/Home/H10000/H10100/pmsReportList",
                    url,
                ]
                result = subprocess.run(cmd, capture_output=True, timeout=50)
                if result.returncode != 0:
                    stderr = result.stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exited {result.returncode}")
                if not result.stdout:
                    raise RuntimeError("empty response body")
                return result.stdout.decode("utf-8", errors="replace")
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt + 1}/{retries} failed for {url}: {exc}")
                if attempt < retries - 1:
                    time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
        return None

    @staticmethod
    def _make_soup(raw):
        """Build BeautifulSoup with html5lib -> lxml -> html.parser fallback."""
        try:
            from bs4 import BeautifulSoup
        except Exception:
            return None

        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[gi-re-kr-home] BeautifulSoup({parser}) failed: {exc}")
                continue
        return None

    @staticmethod
    def _clean_text(text):
        text = re.sub(r"\s+", " ", text or "")
        return text.strip()

    @classmethod
    def _text(cls, node):
        if not node:
            return ""
        return cls._clean_text(node.get_text(" ", strip=True))

    @staticmethod
    def _absolute_url(url):
        if not url:
            return None
        return urllib.parse.urljoin("https://www.gi.re.kr/Home/H10000/H10100/", url)

    @staticmethod
    def _normalize_year_date(raw):
        """Convert Korean year/date strings to a stable ISO-like date."""
        if not raw:
            return None
        text = re.sub(r"\s+", "", str(raw))
        m = re.search(r"(\d{4})년(?:도)?", text)
        if m:
            return f"{m.group(1)}-01-01"
        m = re.search(r"(\d{4})[./-](\d{1,2})(?:[./-](\d{1,2}))?", text)
        if m:
            day = m.group(3) or "1"
            return f"{m.group(1)}-{m.group(2).zfill(2)}-{day.zfill(2)}"
        m = re.search(r"(\d{4})(\d{2})(\d{2})", text)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return str(raw).strip()

    @staticmethod
    def _split_people(raw):
        if not raw:
            return []
        parts = re.split(r"[,;/·ㆍ]|(?:\s+외\s+)", raw)
        return [p.strip() for p in parts if p.strip()]

    @staticmethod
    def _attachment_name(label):
        label = re.sub(r"\([^)]*(?:kB|KB|MB|bytes?)[^)]*\)\s*$", "", label or "", flags=re.I)
        return re.sub(r"\s+", " ", label).strip() or None

    @staticmethod
    def _filename_from_url(url):
        if not url:
            return None
        path = urllib.parse.urlparse(url).path
        tail = os.path.basename(path)
        if "." in tail and len(tail) <= 220:
            return urllib.parse.unquote(tail)
        return None

    # ------------------------------------------------------------------
    # Parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw, page):
        soup = self._make_soup(raw)
        if soup is None:
            return [], False

        items = []
        for a_tag in soup.select("a.m-board[href*='pmsReportView?projcd=']"):
            href = a_tag.get("href") or ""
            parsed_href = urllib.parse.urlparse(href)
            qs = urllib.parse.parse_qs(parsed_href.query)
            projcd = (qs.get("projcd") or [""])[0].strip()
            if not projcd:
                m = re.search(r"projcd=([^&]+)", href)
                projcd = urllib.parse.unquote(m.group(1)).strip() if m else ""
            if not projcd:
                continue

            detail_url = self._absolute_url(f"pmsReportView?projcd={urllib.parse.quote(projcd)}")
            title = self._text(a_tag.select_one(".m-board-title"))
            category = self._text(a_tag.select_one(".m-board-tag"))
            authors = ""
            year_raw = ""

            for info in a_tag.select(".m-board-info"):
                label = self._text(info.select_one(".m-board-info-title"))
                body = self._text(info.select_one(".m-board-info-body"))
                if "저자" in label:
                    authors = body
                elif "발행" in label:
                    year_raw = body

            items.append({
                "projcd": projcd,
                "post_number": projcd,
                "url": detail_url,
                "title": title,
                "category": category,
                "authors_raw": authors,
                "published_raw": year_raw,
                "listed_raw": year_raw,
                "page": page,
            })

        next_link = soup.select_one(".m-pagination-next[href]:not([href='javascript:;'])")
        has_next = bool(next_link)
        return items, has_next

    def _parse_detail(self, raw, item):
        soup = self._make_soup(raw)
        if soup is None:
            return {}

        result = {}
        header = soup.select_one(".m-boardDetail-header")
        if header:
            result["title"] = self._text(header.select_one(".m-board-title"))
            result["category"] = self._text(header.select_one(".m-board-tag"))
            for info in header.select(".m-boardDetail-info"):
                label = self._text(info.select_one(".m-boardDetail-info-title"))
                body = self._text(info.select_one(".m-boardDetail-info-body"))
                if "저자" in label:
                    result["authors_raw"] = body
                elif "발행" in label:
                    result["published_raw"] = body
                elif "조회" in label:
                    result["view_count"] = body

        content = soup.select_one(".m-boardDetail-content")
        if content:
            for bad in content.select("script, style"):
                bad.decompose()
            result["abstract"] = self._text(content)

        attachments = []
        pdf_url = None
        original_filename = None
        for a_tag in soup.select(".m-boardDetail-subHeader a[href], a.pmsFileDown[href]"):
            href = a_tag.get("href") or ""
            name = self._attachment_name(self._text(a_tag))
            full_url = urllib.parse.urljoin(self.base_url, href)
            rec = {"url": full_url, "label": name}
            attachments.append(rec)
            lower = f"{href} {name or ''}".lower()
            if pdf_url is None and ".pdf" in lower:
                pdf_url = full_url
                original_filename = name or self._filename_from_url(full_url)

        if pdf_url is None:
            for rec in attachments:
                if "/attachdown" in rec["url"].lower() and rec.get("label", "").lower().endswith(".pdf"):
                    pdf_url = rec["url"]
                    original_filename = rec["label"] or self._filename_from_url(rec["url"])
                    break

        result["pdf_url"] = pdf_url
        result["original_filename"] = original_filename
        result["attachments"] = attachments

        department = None
        for charger in soup.select(".pageInfo .charger"):
            label = self._text(charger.select_one(".title"))
            body = self._text(charger.select_one(".body"))
            if "담당부서" in label and body:
                department = body
                break
        result["department"] = department
        result["projcd"] = item["projcd"]
        return result

    def _paper_from_item(self, item, detail):
        title = detail.get("title") or item.get("title")
        category = detail.get("category") or item.get("category")
        authors_raw = detail.get("authors_raw") or item.get("authors_raw")
        published_raw = detail.get("published_raw") or item.get("published_raw")
        listed_raw = item.get("listed_raw") or published_raw
        published_date = self._normalize_year_date(published_raw)
        listed_date = self._normalize_year_date(listed_raw)
        abstract = detail.get("abstract") or ""
        pdf_url = detail.get("pdf_url")
        original_filename = detail.get("original_filename")
        authors = self._split_people(authors_raw)
        department = detail.get("department") or "연구지원팀"

        metadata = {
            "projcd": item.get("projcd"),
            "post_number": item.get("post_number"),
            "posted_date": listed_raw,
            "listed_date": listed_date,
            "published_raw": published_raw,
            "category_raw": category,
            "authors_raw": authors_raw,
            "department": department,
            "view_count": detail.get("view_count"),
            "attachments": detail.get("attachments") or [],
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "list_page": item.get("page"),
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": item.get("projcd"),
            "post_number": item.get("post_number"),
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": "; ".join(authors) if authors else None,
            "publisher": "강원연구원",
            "department": department,
            "journal": None,
            "url": item.get("url"),
            "pdf_url": pdf_url,
            "keywords": None,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl report pages until limit, exhaustion, repeat, or safety cap."""
        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else "∞"

        for page in range(1, _MAX_PAGES + 1):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > _MAX_WALL_SECONDS - 30:
                print(f"[{self.site_id}] approaching 25-minute wall-clock budget; exiting cleanly")
                break
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")
            if page == _MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {_MAX_PAGES} pages reached")

            params = {"page": page} if page > 1 else None
            raw = self._curl(_LIST_URL, params=params)
            if not raw:
                print(f"[{self.site_id}] list page {page} fetch failed; stopping")
                break

            items, has_next = self._parse_list_page(raw, page)
            if not items:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url = item.get("url")
                if not url:
                    continue
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl(_DETAIL_URL, params={"projcd": item["projcd"]})
                    if not detail_raw:
                        print(f"[{self.site_id}] item {item.get('projcd')} failed: detail fetch returned empty")
                        continue

                    detail = self._parse_detail(detail_raw, item)
                    paper = self._paper_from_item(item, detail)
                    if not paper.get("title"):
                        print(f"[{self.site_id}] item {item.get('projcd')}: missing title; skipping")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < _MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {item.get('projcd')}: "
                            f"abstract too short ({len(abstract)} chars); skipping"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_or_inf}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('projcd')} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: 0 new records; stopping")
                break
            if not has_next:
                print(f"[{self.site_id}] page {page}: next page absent; stopping")
                break

        print(f"[{self.site_id}] done. saved {saved}")
        return saved
