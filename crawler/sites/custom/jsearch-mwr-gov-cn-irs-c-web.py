# -*- coding: utf-8 -*-
"""Crawler for jsearch.mwr.gov.cn/irs-c-web — 水利易搜 (MWR China unified search portal).

Endpoint: POST http://jsearch.mwr.gov.cn/irs/front/search
  code=18703928938, dataTypeId=26264 (全部资讯 — "All news / info" tab)
  Plain HTTP; no TLS negotiation needed but curl -sk used per project convention.

Article items (displayTemplateId=26151, from www.mwr.gov.cn) carry a `content`
snippet (~200 chars) in the list response which serves as the abstract.
Video items (displayTemplateId=26506, from vod.mwr.gov.cn) have no text content
and are skipped.  For any other non-video item whose list content is too short,
the detail page is fetched and parsed.
"""

import json
import os
import re
import subprocess
import sys
import time

# Absolute import: spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler


class JsearchMwrGovCnCrawler(BaseCrawler):
    """Crawler for 水利易搜 (MWR China: Ministry of Water Resources search portal)."""

    site_id = "jsearch-mwr-gov-cn-irs-c-web"
    site_name = "Custom: jsearch-mwr-gov-cn-irs-c-web"
    base_url = "http://jsearch.mwr.gov.cn"

    _API_URL = "http://jsearch.mwr.gov.cn/irs/front/search"
    _REFERER = "http://jsearch.mwr.gov.cn/irs-c-web/search.shtml?code=18703928938&dataTypeId=26264"
    _CODE = "18703928938"
    _DATA_TYPE_ID = "26264"
    _PAGE_SIZE = 30

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_post(self, payload: dict) -> dict | None:
        """POST JSON via curl with 3-attempt exponential backoff. Returns parsed JSON or None."""
        data_str = json.dumps(payload, ensure_ascii=False)
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-X", "POST",
            "-H", "Content-Type: application/json",
            "-H", f"Referer: {self._REFERER}",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-d", data_str,
            self._API_URL,
        ]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = res.stdout
                if not raw or not raw.strip():
                    if attempt < 2:
                        wait = 3 ** (attempt + 1)
                        print(f"[{self.site_id}] empty response attempt {attempt+1}, retrying in {wait}s")
                        time.sleep(wait)
                    continue
                try:
                    return json.loads(raw.decode("utf-8", errors="replace"))
                except (json.JSONDecodeError, ValueError):
                    if attempt < 2:
                        wait = 3 ** (attempt + 1)
                        print(f"[{self.site_id}] JSON decode error attempt {attempt+1}, retrying in {wait}s")
                        time.sleep(wait)
                    continue
            except subprocess.TimeoutExpired:
                if attempt < 2:
                    wait = 3 ** (attempt + 1)
                    print(f"[{self.site_id}] timeout attempt {attempt+1}, retrying in {wait}s")
                    time.sleep(wait)
            except Exception as exc:
                print(f"[{self.site_id}] curl error attempt {attempt+1}: {exc}")
                if attempt < 2:
                    time.sleep(3 ** (attempt + 1))
        print(f"[{self.site_id}] curl failed after 3 attempts for {self._API_URL}")
        return None

    # ------------------------------------------------------------------
    # Detail-page fetching
    # ------------------------------------------------------------------

    def _fetch_detail(self, url):
        """Fetch a detail page via curl and return its main text content."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--max-time", "20",
            "-A", self.USER_AGENT,
            "-H", f"Referer: {self._REFERER}",
            url,
        ]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
                if res.returncode != 0:
                    raise RuntimeError(f"curl exit {res.returncode}")
                raw = res.stdout.decode("utf-8", errors="replace")
                if not raw.strip():
                    raise RuntimeError("empty response")
                return self._extract_text(raw, url)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] detail attempt {attempt + 1}/3 for {url}: {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return ""

    def _extract_text(self, html, url=""):
        """Parse HTML with BeautifulSoup and return main article text."""
        try:
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, "html5lib")
            except Exception:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, "lxml")
                except Exception:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup failed for {url}: {exc}")
            return ""

        for sel in [
            ".TRS_Editor", ".article-content", ".art-content",
            ".news-content", "#zoom", "#content .content",
            ".main-text", ".article-body", ".page-content",
        ]:
            els = soup.select(sel)
            if els:
                text = " ".join(e.get_text(" ", strip=True) for e in els)
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) >= 100:
                    return text

        # Fallback: collect substantial paragraphs (skip short nav lines)
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p") if len(p.get_text(strip=True)) > 30]
        if paras:
            text = re.sub(r"\s+", " ", " ".join(paras)).strip()
            if len(text) >= 100:
                return text
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the MWR search portal list API and save records to the DB.

        Paginates through /irs/front/search until:
          (a) saved >= limit, (b) page returns empty list,
          (c) 200-page safety cap, or (d) 25-minute wall-clock budget.

        Returns the number of records saved.
        """
        saved = 0
        page = 1
        seen_urls: set = set()
        limit_val = limit if limit is not None else float("inf")
        start_ts = time.time()
        MAX_PAGES = 200
        MAX_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        while saved < limit_val and page <= MAX_PAGES:
            if time.time() - start_ts > MAX_SECS:
                print(f"[{self.site_id}] 25-minute budget reached at page {page}, stopping cleanly.")
                break

            payload = {
                "code": self._CODE,
                "dataTypeId": self._DATA_TYPE_ID,
                "pageNo": page,
                "pageSize": self._PAGE_SIZE,
                "orderBy": "time",
                "searchBy": "all",  # JS default; "all" = full-text search
            }

            resp = self._curl_post(payload)
            if resp is None:
                print(f"[{self.site_id}] page {page}: fetch failed, stopping.")
                break
            if not resp.get("success"):
                print(f"[{self.site_id}] page {page}: API error — {resp.get('msg')}, stopping.")
                break

            result = resp.get("data") or {}
            items = (result.get("middle") or {}).get("list") or []
            pager = result.get("pager") or {}
            page_count = int(pager.get("pageCount") or 1)

            if not items:
                print(f"[{self.site_id}] page {page}: empty list, stopping.")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}/{page_count}: saved {saved}/{limit_val}")

            # Count unseen URLs on this page for dedup-based termination
            unseen_on_page = 0

            for item in items:
                if saved >= limit_val:
                    break

                try:
                    doc_id = str(item.get("documentId") or "")
                    item_url = (item.get("url") or "").strip()
                    title = (item.get("title_no_tag") or item.get("title") or "").strip()
                    abstract = (item.get("content") or "").strip()
                    pub_date = (item.get("time") or item.get("table-6") or "").strip()
                    publisher = (item.get("source") or "").strip()

                    # URL deduplication — guards against paginator looping
                    if item_url and item_url in seen_urls:
                        continue
                    if item_url:
                        seen_urls.add(item_url)
                        unseen_on_page += 1

                    if not title:
                        print(f"[{self.site_id}] item {doc_id}: no title, skipping.")
                        continue

                    # Fetch detail page for items with short/missing list content
                    if len(abstract) < 100 and item_url:
                        if "vod.mwr.gov.cn" in item_url:
                            # Video pages are JS-only — no parseable text
                            print(
                                f"[{self.site_id}] item {doc_id}: "
                                f"video item, no text abstract, skipping."
                            )
                            continue
                        detail = self._fetch_detail(item_url)
                        if detail and len(detail) > len(abstract):
                            abstract = detail
                        time.sleep(self._delay)

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {doc_id}: "
                            f"abstract {len(abstract)} chars (<50), skipping."
                        )
                        continue

                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] item {doc_id}: "
                            f"abstract {len(abstract)} chars (<100), skipping."
                        )
                        continue

                    # Publish date: API returns "YYYY-MM-DD HH:MM:SS" or "YYYY-MM-DD"
                    m = re.search(r"(\d{4}-\d{2}-\d{2})", pub_date)
                    iso_date = m.group(1) if m else None

                    # Raw metadata — all API fields not mapped to top-level columns
                    _skip_in_meta = {"title", "title_no_tag", "content", "url", "time", "source"}
                    raw_meta: dict = {k: v for k, v in item.items() if k not in _skip_in_meta}
                    raw_meta["documentId"] = doc_id
                    raw_meta["posted_date"] = iso_date

                    self._save_paper({
                        "external_id": doc_id,
                        "url": item_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": iso_date,
                        "posted_date": iso_date,
                        "publisher": publisher or None,
                        "authors": None,
                        "pdf_url": None,
                        "keywords": None,
                        "metadata": json.dumps(raw_meta, ensure_ascii=False),
                    })
                    saved += 1

                except Exception as exc:
                    doc_id_str = item.get("documentId", "?")
                    print(f"[{self.site_id}] item {doc_id_str} failed: {exc}")
                    continue

            # Stop only when the page is entirely duplicate (URL-dedup based)
            if unseen_on_page == 0 and items:
                print(f"[{self.site_id}] page {page}: all items already seen, stopping.")
                break

            if page >= page_count:
                print(f"[{self.site_id}] reached last page ({page_count}), done.")
                break

            if page == MAX_PAGES:
                print(f"[{self.site_id}] safety cap {MAX_PAGES} pages reached, stopping.")
                break

            page += 1
            time.sleep(0.5)  # light rate-limit between page fetches

        print(f"[{self.site_id}] crawl complete: saved {saved} records total.")
        return saved
