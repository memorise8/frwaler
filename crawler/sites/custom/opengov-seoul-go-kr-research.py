# -*- coding: utf-8 -*-
"""Seoul Metropolitan Government open-document portal — 정책연구자료 (policy research).

Starting URL: https://opengov.seoul.go.kr/research/list
List pages are a Drupal view (``?items_per_page=50&page=N``, 1-indexed).
Each item's detail page (``/research/{nid}``) carries the abstract, a
metadata table (등록일/생산일/제공부서/원본시스템/...), and attached files.
"""

from __future__ import annotations

import json
import os
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


def _make_soup(html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _norm_date(raw: str) -> str | None:
    """Normalize a date string to ISO ``YYYY-MM-DD``. Accepts ``YYYYMMDD`` or already-ISO."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"^(\d{4})(\d{2})(\d{2})$", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


class OpengovSeoulGoKrResearchCrawler(BaseCrawler):
    """Crawler for opengov.seoul.go.kr's 정책연구자료 (policy research) archive."""

    site_id = "opengov-seoul-go-kr-research"
    site_name = "Custom: opengov-seoul-go-kr-research"
    base_url = "https://opengov.seoul.go.kr"

    _LIST_URL = "https://opengov.seoul.go.kr/research/list"
    _MIN_ABSTRACT = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _ITEMS_PER_PAGE = 50

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _list_page_url(self, page: int) -> str:
        if page <= 1:
            return f"{self._LIST_URL}?items_per_page={self._ITEMS_PER_PAGE}"
        return f"{self._LIST_URL}?items_per_page={self._ITEMS_PER_PAGE}&page={page}"

    def _parse_list_page(self, raw_html: str) -> list:
        """Return a list of dicts: {nid, url, title, listed_date, dept, category}."""
        entries = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error on list page: {exc}")
            return entries

        wrap = soup.select_one(".table-list-wrap")
        if not wrap:
            return entries

        for li in wrap.find_all("li"):
            try:
                a = li.select_one(".title-area a[href]")
                if not a:
                    continue
                href = (a.get("href") or "").strip()
                m = re.search(r"/research/(\d+)", href)
                if not m:
                    continue
                nid = m.group(1)
                url = href if href.startswith("http") else f"{self.base_url}{href}"

                title_span = a.find("span")
                title = title_span.get_text(strip=True) if title_span else a.get_text(strip=True)

                cat_span = li.select_one(".title-category span")
                category = cat_span.get_text(strip=True) if cat_span else ""

                date_span = li.select_one(".title-info .date")
                listed_date_raw = ""
                if date_span:
                    listed_date_raw = date_span.get_text(strip=True).split(":")[-1].strip()

                dept_span = li.select_one(".title-info .dept")
                dept = ""
                if dept_span:
                    dept = dept_span.get_text(strip=True).split(":")[-1].strip()

                entries.append({
                    "nid": nid,
                    "url": url,
                    "title": title,
                    "listed_date_raw": listed_date_raw,
                    "category": category,
                    "dept": dept,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
                continue

        return entries

    def _parse_detail_page(self, raw_html: str, nid: str) -> dict:
        """Parse a detail page into a flat dict of extracted fields."""
        soup = _make_soup(raw_html)

        og_title = soup.find("meta", attrs={"property": "og:title"})
        title = (og_title.get("content") or "").strip() if og_title else ""
        if not title:
            title_tag = soup.find("title")
            if title_tag:
                title = title_tag.get_text(strip=True).split(">")[0].strip()

        abstract = ""
        desc_h4 = soup.find("h4", string=re.compile("문서 설명"))
        if desc_h4:
            container = desc_h4.find_parent("div")
            if container:
                line_all = container.find("div", class_="line-all")
                if line_all:
                    abstract = line_all.get_text(" ", strip=True)
                    abstract = re.sub(r"^초록\s*", "", abstract)
                    abstract = re.sub(r"\s+", " ", abstract).strip()

        info = {}
        table = soup.select_one("table.table-response") or soup.select_one(".table-wrap table")
        if table:
            for tr in table.find_all("tr"):
                cells = tr.find_all(["th", "td"])
                i = 0
                while i < len(cells) - 1:
                    if cells[i].name == "th":
                        key = cells[i].get_text(strip=True)
                        val = cells[i + 1].get_text(strip=True) if i + 1 < len(cells) else ""
                        if key:
                            info[key] = val
                        i += 2
                    else:
                        i += 1

        pdf_url = None
        original_filename = None
        attach_ul = soup.select_one("ul.list-attachment")
        if attach_ul:
            attachments = []
            for li in attach_ul.find_all("li"):
                dl = li.select_one("a.btn-download")
                title_down = li.select_one(".title-down")
                if not dl or not dl.get("href") or not title_down:
                    continue
                href = dl["href"].strip()
                url = href if href.startswith("http") else f"{self.base_url}{href}"
                fname_raw = title_down.get_text(strip=True)
                fname = re.sub(r"\s*\([\d.]+\s*[KMGT]?B\)\s*$", "", fname_raw).strip()
                attachments.append((url, fname or None))

            if attachments:
                # Prefer an actual .pdf attachment over cover-image thumbnails
                # (some items list a .png/.jpg cover before the real PDF).
                pdf_attachment = next(
                    (a for a in attachments if a[1] and a[1].lower().endswith(".pdf")),
                    None,
                )
                pdf_url, original_filename = pdf_attachment or attachments[0]

        return {
            "title": title,
            "abstract": abstract,
            "info": info,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl opengov.seoul.go.kr's 정책연구자료 list, page by page, saving qualifying items."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        lim_str = str(limit) if limit is not None else "inf"

        try:
            for page in range(1, self._MAX_PAGES + 1):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                list_url = self._list_page_url(page)
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                    break

                entries = self._parse_list_page(raw)
                if not entries:
                    print(f"[{self.site_id}] page {page}: no entries found, stopping pagination.")
                    break

                new_entries = [e for e in entries if e["url"] not in seen_urls]
                if not new_entries:
                    print(f"[{self.site_id}] page {page}: 0 new records (all seen), stopping.")
                    break

                for entry in new_entries:
                    if limit is not None and saved >= limit:
                        break
                    if time.time() - start_time > self._MAX_WALL:
                        print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                        break

                    seen_urls.add(entry["url"])

                    try:
                        detail_raw = self._curl_get(entry["url"])
                        if not detail_raw:
                            print(f"[{self.site_id}] item {entry['nid']} failed: no detail response; skipping.")
                            continue

                        parsed = self._parse_detail_page(detail_raw, entry["nid"])

                        title = parsed["title"] or entry["title"]
                        if not title:
                            print(f"[{self.site_id}] item {entry['nid']} failed: empty title; skipping.")
                            continue

                        abstract = parsed["abstract"]
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] item {entry['nid']}: short abstract "
                                f"({len(abstract)} chars) for '{title[:50]}', skipping."
                            )
                            continue

                        info = parsed["info"]
                        listed_date = (
                            _norm_date(info.get("등록일", ""))
                            or _norm_date(entry.get("listed_date_raw", ""))
                        )
                        published_date = _norm_date(info.get("생산일", "")) or listed_date

                        publisher = info.get("원본시스템", "") or entry.get("category", "")
                        department = info.get("제공부서", "") or entry.get("dept", "")
                        author = info.get("작성자(책임자)", "")
                        category = info.get("분야", "") or entry.get("category", "")

                        meta = {
                            "posted_date": entry.get("listed_date_raw") or info.get("등록일", ""),
                            "originalFilename": parsed.get("original_filename"),
                            "node_id": entry["nid"],
                            "관리번호": info.get("관리번호"),
                            "유형": info.get("유형"),
                            "생산년도": info.get("생산년도"),
                            "지역": info.get("지역"),
                            "소요예산": info.get("소요예산"),
                            "이용조건": info.get("이용조건"),
                        }
                        meta = {k: v for k, v in meta.items() if v}

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": entry["nid"],
                            "post_number": entry["nid"],
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": author,
                            "publisher": publisher,
                            "department": department,
                            "journal": None,
                            "url": entry["url"],
                            "pdf_url": parsed.get("pdf_url"),
                            "keywords": "",
                            "category": category,
                            "doi": None,
                            "original_filename": parsed.get("original_filename"),
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                        time.sleep(self._delay)

                    except Exception as exc:
                        print(f"[{self.site_id}] item {entry['nid']} failed: {exc}; continuing.")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            else:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
