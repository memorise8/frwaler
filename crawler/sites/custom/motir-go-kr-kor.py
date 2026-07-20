# -*- coding: utf-8 -*-
"""Ministry of Trade, Industry and Energy (motir.go.kr) press-release crawler.

Starting URL: https://www.motir.go.kr/kor/article/ATCL3f49a5a8c
The ATCL id identifies a board (게시판); listing is paginated via
``?pageIndex=N`` on the same URL, and each item's detail page lives at
``/kor/article/{ATCL_CODE}/{bbsSeqN}/view``.
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


_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DIST_DATE_RE = re.compile(r"배포\s*(\d{4})\s*[.\s]\s*(\d{1,2})\s*[.\s]\s*(\d{1,2})")
_VIEW_ID_RE = re.compile(r"article\.view\('(\d+)'\)")
_JS_HREF_RE = re.compile(r"location\.href='([^']+)'")


class MotirGoKrKorCrawler(BaseCrawler):
    """Crawler for motir.go.kr (산업통상부) 보도·참고자료 board."""

    site_id = "motir-go-kr-kor"
    site_name = "Custom: motir-go-kr-kor"
    base_url = "https://www.motir.go.kr"

    _ARTICLE_CODE = "ATCL3f49a5a8c"
    _LIST_URL = f"{base_url}/kor/article/{_ARTICLE_CODE}"
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds
    _PUBLISHER = "산업통상부"

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

    def _parse_list_page(self, raw_html: str) -> list:
        """Parse one list page into row dicts. Returns [] if the page has no rows."""
        rows = []
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] list HTML parse error: {exc}")
            return rows

        table = soup.find("table", id="mytable")
        if table is None:
            return rows
        tbody = table.find("tbody")
        if tbody is None:
            return rows

        for tr in tbody.find_all("tr", recursive=False):
            try:
                anchor = tr.find("a", href=lambda h: bool(h) and "article.view" in h)
                if anchor is None:
                    continue
                m = _VIEW_ID_RE.search(anchor.get("href") or "")
                if not m:
                    continue
                bbs_seq_n = m.group(1)
                title = anchor.get_text(strip=True)
                if not title:
                    continue

                badge = tr.find("span", class_="badge-category")
                category = badge.get_text(strip=True) if badge else ""

                tds = tr.find_all("td", recursive=False)
                title_td = anchor.find_parent("td")
                listed_date = ""
                department = ""
                for td in tds:
                    if td is title_td:
                        continue
                    text = td.get_text(strip=True)
                    if not text:
                        continue
                    if _DATE_RE.fullmatch(text):
                        listed_date = text
                        continue
                    if re.fullmatch(r"[\d,]+", text):
                        continue  # 번호 / 조회수
                    if category and text == category:
                        continue
                    if not department:
                        department = text

                rows.append({
                    "bbs_seq_n": bbs_seq_n,
                    "title": title,
                    "category": category,
                    "listed_date": listed_date,
                    "department": department,
                })
            except Exception as exc:
                print(f"[{self.site_id}] row parse error: {exc}")
                continue

        return rows

    def _parse_detail_page(self, raw_html: str) -> dict:
        """Parse a detail page into a dict of fields. Returns {} on hard failure."""
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] detail HTML parse error: {exc}")
            return {}

        title_el = soup.select_one(".detail-tit span") or soup.select_one(".detail-tit")
        title = title_el.get_text(strip=True) if title_el else ""

        info = soup.select_one(".detail-info")
        fields = {}
        if info:
            for li in info.select("li"):
                em = li.find("em")
                span = li.find("span")
                if em and span:
                    fields[em.get_text(strip=True)] = span.get_text(strip=True)

        listed_date = fields.get("등록일", "")
        department = fields.get("담당부서", "")
        contact_person = fields.get("담당자", "")
        contact_phone = fields.get("연락처", "")

        cont = soup.select_one(".detail-cont")
        abstract = cont.get_text(" ", strip=True) if cont else ""
        abstract = re.sub(r"\s+", " ", abstract).strip()

        published_date = listed_date
        dist_m = _DIST_DATE_RE.search(abstract)
        if dist_m:
            y, mo, d = dist_m.groups()
            published_date = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"

        attachments = []
        for a in soup.select(".info-down a"):
            href = a.get("href", "") or ""
            js_m = _JS_HREF_RE.search(href)
            real_url = js_m.group(1) if js_m else (href if href.startswith("http") or href.startswith("/") else "")
            if not real_url or "attach/down" not in real_url:
                continue
            text = a.get_text(strip=True)
            filename = re.sub(r"\s*\[[^\[\]]*\]\s*$", "", text).strip()
            attachments.append({
                "url": real_url if real_url.startswith("http") else self.base_url + real_url,
                "filename": filename,
            })

        pdf_url = None
        original_filename = None
        for att in attachments:
            if att["filename"].lower().endswith(".pdf"):
                pdf_url = att["url"]
                original_filename = att["filename"]
                break
        if pdf_url is None and attachments:
            original_filename = attachments[0]["filename"]

        return {
            "title": title,
            "abstract": abstract,
            "listed_date": listed_date,
            "published_date": published_date,
            "department": department,
            "contact_person": contact_person,
            "contact_phone": contact_phone,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "attachments": attachments,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl motir.go.kr press releases page by page."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 0

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

                list_url = f"{self._LIST_URL}?pageIndex={page}"
                raw = self._curl_get(list_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page}. Stopping.")
                    break

                rows = self._parse_list_page(raw)
                if not rows:
                    print(f"[{self.site_id}] No rows on page {page}. End of pagination.")
                    break

                new_on_page = 0
                for row in rows:
                    if limit is not None and saved >= limit:
                        break

                    detail_url = f"{self._LIST_URL}/{row['bbs_seq_n']}/view"
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_on_page += 1

                    try:
                        time.sleep(self._delay)
                        detail_raw = self._curl_get(detail_url)
                        if not detail_raw:
                            print(f"[{self.site_id}] Failed to fetch detail {detail_url}, skipping.")
                            continue

                        detail = self._parse_detail_page(detail_raw)
                        if not detail:
                            print(f"[{self.site_id}] Empty parse for {detail_url}, skipping.")
                            continue

                        title = detail.get("title") or row["title"]
                        if not title:
                            print(f"[{self.site_id}] Empty title for {detail_url}, skipping.")
                            continue

                        abstract = detail.get("abstract") or ""
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] Short abstract ({len(abstract)}) for '{title[:50]}', skipping.")
                            continue

                        listed_date = detail.get("listed_date") or row["listed_date"] or None
                        published_date = detail.get("published_date") or listed_date
                        department = detail.get("department") or row["department"] or None

                        meta = {
                            "posted_date": listed_date,
                            "bbsSeqN": row["bbs_seq_n"],
                            "atclId": self._ARTICLE_CODE,
                            "category_raw": row["category"],
                            "department": department,
                            "contact_person": detail.get("contact_person") or None,
                            "contact_phone": detail.get("contact_phone") or None,
                            "attachments": detail.get("attachments") or [],
                        }
                        if detail.get("original_filename"):
                            meta["originalFilename"] = detail["original_filename"]

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": row["bbs_seq_n"],
                            "post_number": row["bbs_seq_n"],
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": None,
                            "publisher": self._PUBLISHER,
                            "department": department,
                            "journal": None,
                            "url": detail_url,
                            "pdf_url": detail.get("pdf_url"),
                            "keywords": None,
                            "category": row["category"] or None,
                            "doi": None,
                            "original_filename": detail.get("original_filename"),
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {row.get('bbs_seq_n', '?')} failed: {exc}; continuing.")
                        continue

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                if new_on_page == 0:
                    print(f"[{self.site_id}] Page {page} had only already-seen rows. Stopping to avoid loop.")
                    break

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
