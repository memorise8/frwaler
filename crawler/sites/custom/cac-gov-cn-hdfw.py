# -*- coding: utf-8 -*-
"""中央网络安全和信息化委员会办公室 举报公告 crawler.

List API:  POST https://www.cac.gov.cn/cms/JsonList
Detail:    HTML  https://www.cac.gov.cn/YYYY-MM/DD/c_<id>.htm
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class CacGovCnHdfwCrawler(BaseCrawler):
    """Crawler for CAC 举报公告 (中央网信办举报中心)."""

    site_id = "cac-gov-cn-hdfw"
    site_name = "Custom: cac-gov-cn-hdfw"
    base_url = "https://www.cac.gov.cn"

    _CHANNEL_CODE = "A09380302"
    _PAGE_SIZE = 20
    _LIST_API = "https://www.cac.gov.cn/cms/JsonList"

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl; returns decoded text or None on failure."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Referer: https://www.cac.gov.cn/",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[cac-gov-cn-hdfw] Empty GET, retry in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[cac-gov-cn-hdfw] curl GET error ({exc}), retry in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[cac-gov-cn-hdfw] curl GET failed after 3 attempts: {exc}")
        return None

    def _curl_post_list(self, pageno: int) -> str | None:
        """POST to JsonList API for one page; returns text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Referer: https://www.cac.gov.cn/hdfw/jbzx/jbgg/A09380302index_1.htm",
            "-d", f"channelCode={self._CHANNEL_CODE}",
            "-d", f"perPage={self._PAGE_SIZE}",
            "-d", f"pageno={pageno}",
            "-d", "condition=0",
            "-d", "fuhao=%3D",
            "-d", "value=",
            self._LIST_API,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout.decode("utf-8", errors="replace")
                if raw.strip():
                    return raw
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[cac-gov-cn-hdfw] Empty POST page {pageno}, retry in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = 3 ** attempt
                    print(f"[cac-gov-cn-hdfw] curl POST error ({exc}), retry in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[cac-gov-cn-hdfw] curl POST failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html: str) -> str:
        text = re.sub(r"<[^>]+>", " ", html)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&#\d+;", " ", text)
        text = re.sub(r"&[a-zA-Z]+;", " ", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Extract YYYY-MM-DD from '2025-12-30 17:15:00.0'."""
        if not raw:
            return ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw.strip())
        return m.group(1) if m else ""

    @staticmethod
    def _extract_id(infourl: str) -> str:
        """Extract numeric ID from '.../c_1768823203908318.htm'."""
        m = re.search(r"c_(\d+)\.htm", infourl)
        return m.group(1) if m else ""

    @staticmethod
    def _normalize_url(infourl: str) -> str:
        if infourl.startswith("//"):
            return "https:" + infourl
        if infourl.startswith("/"):
            return "https://www.cac.gov.cn" + infourl
        return infourl

    def _fetch_detail_text(self, url: str) -> str:
        """Fetch detail page; return body text from BodyLabel or main-content."""
        raw = self._curl_get(url)
        if not raw:
            return ""

        # Try BeautifulSoup with parser fallback chain
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(raw, parser)
                # Prefer BodyLabel
                body = soup.find(id="BodyLabel")
                if body:
                    return body.get_text(separator=" ", strip=True)
                # Fallback to main-content div
                mc = soup.find(class_="main-content")
                if mc:
                    return mc.get_text(separator=" ", strip=True)
                break
            except Exception:
                continue

        # Regex fallback: grab everything inside id=BodyLabel
        m = re.search(
            r'id=["\']?BodyLabel["\']?[^>]*>([\s\S]*?)</(?:div|DIV)>',
            raw, re.IGNORECASE,
        )
        if m:
            return self._strip_html(m.group(1))

        m = re.search(
            r'class=["\']?main-content["\']?[^>]*>([\s\S]*?)</(?:div|DIV)>',
            raw, re.IGNORECASE,
        )
        if m:
            return self._strip_html(m.group(1))

        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl 举报公告 and persist to DB.

        Paginates the JsonList API, fetches each detail page for full text,
        and saves via _save_paper.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        MAX_PAGES = 200
        start_time = time.time()
        MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        while True:
            if limit is not None and saved >= limit:
                break

            if page > MAX_PAGES:
                print(f"[cac-gov-cn-hdfw] Reached safety cap of {MAX_PAGES} pages. Stopping.")
                break

            if time.time() - start_time > MAX_WALL_SECS:
                print("[cac-gov-cn-hdfw] Approaching 25-minute wall-clock budget. Exiting cleanly.")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[cac-gov-cn-hdfw] page {page}: saved {saved}/{limit_str}")

            raw = self._curl_post_list(page)
            if not raw:
                print(f"[cac-gov-cn-hdfw] No response for page {page}. Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[cac-gov-cn-hdfw] JSON decode error at page {page}: {exc}. Stopping.")
                break

            items = data.get("list", [])
            if not items:
                print(f"[cac-gov-cn-hdfw] No items on page {page}. Done.")
                break

            if page == 1:
                total = data.get("totalRec", "?")
                print(f"[cac-gov-cn-hdfw] Total records reported: {total}")

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                infourl_raw = item.get("infourl", "")
                if not infourl_raw:
                    continue

                url = self._normalize_url(infourl_raw)

                if url in seen_urls:
                    continue
                seen_urls.add(url)
                new_on_page += 1

                try:
                    external_id = self._extract_id(url)
                    title = item.get("topic", "").strip()
                    description = item.get("description", "").strip()
                    pubtime = item.get("pubtime", "")
                    filltime = item.get("filltime", "")
                    published_date = self._parse_date(pubtime)
                    listed_date = self._parse_date(filltime)
                    coverurl = item.get("coverurl", "")
                    if coverurl and coverurl.startswith("//"):
                        coverurl = "https:" + coverurl

                    # Fetch full article text
                    time.sleep(self._delay)
                    abstract = self._fetch_detail_text(url)

                    # If abstract is still too short, try description from list
                    if len(abstract) < 50 and len(description) >= 50:
                        abstract = description

                    if len(abstract) < 50:
                        print(
                            f"[cac-gov-cn-hdfw] Short abstract ({len(abstract)} chars)"
                            f" for {url}, skipping."
                        )
                        continue

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "url": url,
                        "pdf_url": None,
                        "authors": None,
                        "publisher": "中央网络安全和信息化委员会办公室",
                        "department": None,
                        "journal": None,
                        "keywords": None,
                        "category": "举报公告",
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": filltime,
                                "pubtime": pubtime,
                                "filltime": filltime,
                                "coverurl": coverurl,
                                "description": description,
                                "channel_code": self._CHANNEL_CODE,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[cac-gov-cn-hdfw] Saved {saved}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[cac-gov-cn-hdfw] item {url} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[cac-gov-cn-hdfw] Page {page} had no new items. Pagination ended.")
                break

            page += 1

        print(f"[cac-gov-cn-hdfw] Done. Total saved: {saved}")
        return saved
