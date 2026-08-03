# -*- coding: utf-8 -*-
"""NPS 국민연금공단 보도자료 crawler.

Starting URL: https://www.nps.or.kr/pnsgdnc/nscvrgdata/getOHAE0002M0List.do?menuId=MN24000898

List pagination: POST pageIndex=N (10 items / page).
Detail URL:      /pnsgdnc/nscvrgdata/getOHAE0002M1.do?pstId=ZZ...
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_BASE_URL  = "https://www.nps.or.kr"
_LIST_URL  = f"{_BASE_URL}/pnsgdnc/nscvrgdata/getOHAE0002M0List.do"
_VIEW_URL  = f"{_BASE_URL}/pnsgdnc/nscvrgdata/getOHAE0002M1.do"
_MENU_ID   = "MN24000898"
_HMPG_CD   = "01"
_BBS_CD    = "BS20240145"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _strip_html(html: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;",  " ",  text)
    text = re.sub(r"&amp;",   "&",  text)
    text = re.sub(r"&lt;",    "<",  text)
    text = re.sub(r"&gt;",    ">",  text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# crawler class
# ---------------------------------------------------------------------------

class NpsOrKrPnsgdncCrawler(BaseCrawler):
    """Crawler for NPS 국민연금공단 보도자료."""

    site_id   = "nps-or-kr-pnsgdnc"
    site_name = "Custom: nps-or-kr-pnsgdnc"
    base_url  = _BASE_URL

    _MAX_PAGES   = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock budget

    # ------------------------------------------------------------------
    # network helpers
    # ------------------------------------------------------------------

    def _curl_post(self, url: str, data: dict, retries: int = 3) -> str | None:
        """POST via curl; return decoded response text or None."""
        body = "&".join(f"{k}={v}" for k, v in data.items())
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-X", "POST",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"Referer: {_LIST_URL}?menuId={_MENU_ID}",
            "--data", body,
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 3 ** attempt
                print(f"[{self.site_id}] empty POST response (attempt {attempt+1}), retry in {wait}s…")
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] curl POST error: {exc}, retry in {wait}s…")
                if attempt < retries - 1:
                    time.sleep(wait)
        return None

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """GET via curl; return decoded response text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept-Language: ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "-H", f"Referer: {_LIST_URL}?menuId={_MENU_ID}",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                wait = 3 ** attempt
                print(f"[{self.site_id}] empty GET response (attempt {attempt+1}), retry in {wait}s…")
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] curl GET error: {exc}, retry in {wait}s…")
                if attempt < retries - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # page parsers
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Extract list items from the board list HTML."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup parse error on list page: {exc}")
            return []
        if soup is None:
            return []

        items = []
        for li in soup.select("li.list-item"):
            try:
                # title + URL
                a_tag = li.select_one("p.title a")
                if not a_tag:
                    continue
                title = a_tag.get_text(" ", strip=True)
                href  = a_tag.get("href", "")
                if not href:
                    continue
                # pstId
                m = re.search(r"pstId=(ZZ\w+)", href)
                if not m:
                    continue
                pst_id = m.group(1)

                full_url = _BASE_URL + href if href.startswith("/") else href

                # summary (may be HTML-encoded)
                summary_tag = li.select_one("p.summary")
                summary = _strip_html(str(summary_tag)) if summary_tag else ""

                # date
                date_tag = li.select_one("li.date span.data")
                date_str = date_tag.get_text(strip=True) if date_tag else ""
                # normalise "2026/05/08" → "2026-05-08"
                published_date = date_str.replace("/", "-")

                # department
                writer_tag = li.select_one("li.writer span.data")
                department = writer_tag.get_text(strip=True) if writer_tag else ""

                items.append({
                    "pst_id":         pst_id,
                    "title":          title,
                    "url":            full_url,
                    "summary":        summary,
                    "published_date": published_date,
                    "department":     department,
                })
            except Exception as exc:
                print(f"[{self.site_id}] list item parse error: {exc}")
                continue

        return items

    def _parse_detail_page(self, html: str) -> dict:
        """Extract full content, author, and file list from the detail HTML."""
        result = {
            "abstract": "",
            "authors":  [],
            "files":    [],
        }
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup parse error on detail page: {exc}")
            return result
        if soup is None:
            return result

        # Full content body
        content_div = soup.select_one("div.view-contents-wrap")
        if content_div:
            result["abstract"] = _strip_html(str(content_div))

        # Author from the info table (작성자 row)
        author_th = soup.find("th", string=re.compile(r"작성자"))
        if author_th:
            td = author_th.find_next_sibling("td")
            if td:
                name = td.get_text(strip=True)
                if name:
                    result["authors"] = [name]

        # Attached file names (hwp / pdf / etc.)
        for p in soup.select("p.a-file"):
            fname = p.get("title", "") or p.get_text(strip=True)
            if fname:
                result["files"].append(fname)

        return result

    # ------------------------------------------------------------------
    # main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl NPS 보도자료 list → detail pages and save."""
        saved       = 0
        seen_urls   = set()
        start_time  = time.monotonic()
        limit_label = str(limit) if limit is not None else "∞"

        for page in range(1, self._MAX_PAGES + 1):

            # --- wall-clock budget check ---
            if time.monotonic() - start_time > self._MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}. Stopping.")
                break

            # --- limit check ---
            if limit is not None and saved >= limit:
                break

            # --- progress log every 10 pages ---
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

            # --- fetch list page ---
            post_data = {
                "menuId":     _MENU_ID,
                "hmpgCd":     _HMPG_CD,
                "hmpgBbsCd":  _BBS_CD,
                "pageIndex":  str(page),
                "sortSe":     "FR",
                "searchGbu":  "",
                "searchText": "",
            }
            raw = self._curl_post(_LIST_URL, post_data)
            if raw is None:
                print(f"[{self.site_id}] page {page}: failed to fetch list. Skipping.")
                continue

            items = self._parse_list_page(raw)
            if not items:
                print(f"[{self.site_id}] page {page}: no items found. End of pagination.")
                break

            # --- iterate items ---
            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                url    = item["url"]
                pst_id = item["pst_id"]

                # deduplicate
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    # rate-limit before detail fetch
                    time.sleep(self._delay)

                    # fetch detail
                    detail_html = self._curl_get(url)
                    if detail_html is None:
                        print(f"[{self.site_id}] item {pst_id}: detail fetch failed, skipping.")
                        continue

                    detail = self._parse_detail_page(detail_html)

                    # combine summary + full content for abstract
                    parts = []
                    if item["summary"]:
                        parts.append(item["summary"])
                    if detail["abstract"] and detail["abstract"] != item["summary"]:
                        parts.append(detail["abstract"])
                    abstract = "\n\n".join(parts)

                    # skip items with very short abstracts
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] item {pst_id}: abstract too short ({len(abstract)} chars), skipping.")
                        continue

                    authors = detail["authors"] or []

                    paper = {
                        "id":             None,
                        "site_id":        self.site_id,
                        "external_id":    pst_id,
                        "title":          item["title"],
                        "authors":        json.dumps(authors, ensure_ascii=False),
                        "abstract":       abstract,
                        "category":       "보도자료",
                        "keywords":       json.dumps([], ensure_ascii=False),
                        "published_date": item["published_date"],
                        "url":            url,
                        "pdf_url":        "",
                        "doi":            "",
                        "department":     item["department"],
                        "metadata":       json.dumps({
                            "hmpgBbsCd":     _BBS_CD,
                            "attachedFiles": detail["files"],
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_label}: {item['title'][:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pst_id} failed: {exc}. Continuing.")
                    continue

            # if every item on this page was already seen → end of new content
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen. Stopping.")
                break

        else:
            # safety cap reached
            print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
