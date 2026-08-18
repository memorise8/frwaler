# -*- coding: utf-8 -*-
"""Data.gov.au (Australian government open data portal) PDF-resource crawler.

Starting URL: https://data.gov.au/data/dataset/?res_format=PDF

The site's front end (data.gov.au/data/dataset/) is a CKAN-backed catalog
that still exposes the legacy CKAN action API under
``https://data.gov.au/data/api/3/action/``. ``package_search`` returns full
dataset records (title, description, dates, organization, resources) in a
single JSON call, so no separate detail-page fetch is required per item.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class DataGovAuDataCrawler(BaseCrawler):
    """Crawler for data.gov.au datasets that include a PDF resource."""

    site_id = "data-gov-au-data"
    site_name = "Custom: data-gov-au-data"
    base_url = "https://data.gov.au"
    DELIVERY_ORDER = "arbitrary"

    _API_URL = "https://data.gov.au/data/api/3/action/package_search"
    _DATASET_URL = "https://data.gov.au/data/dataset/{name}"
    _ROWS_PER_PAGE = 100
    _MIN_ABSTRACT = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict | None = None) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        full_url = url
        if params:
            from urllib.parse import urlencode
            full_url = f"{url}?{urlencode(params)}"

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

    def _fetch_page(self, start: int):
        """Fetch one page of package_search results. Returns list of dataset dicts."""
        raw = self._curl_get(self._API_URL, params={
            "fq": "res_format:PDF",
            "rows": self._ROWS_PER_PAGE,
            "start": start,
        })
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] JSON decode error at start={start}: {exc}")
            return []
        if not data.get("success"):
            print(f"[{self.site_id}] API returned success=false at start={start}")
            return []
        return (data.get("result") or {}).get("results") or []

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iso_date(raw: str | None) -> str | None:
        """Truncate a CKAN ISO timestamp ('2013-05-12T10:03:35.572069') to YYYY-MM-DD."""
        if not raw or not isinstance(raw, str):
            return None
        return raw[:10] if len(raw) >= 10 else None

    @staticmethod
    def _pick_pdf_resource(resources: list) -> dict | None:
        for res in resources or []:
            fmt = (res.get("format") or "").strip().upper()
            if fmt == "PDF":
                return res
        return None

    @staticmethod
    def _original_filename(pdf_res: dict) -> str | None:
        if not pdf_res:
            return None
        url = pdf_res.get("url") or ""
        from urllib.parse import urlparse, unquote
        path = urlparse(url).path
        segment = unquote(path.rsplit("/", 1)[-1]) if path else ""
        if segment and segment.lower().endswith(".pdf"):
            return segment
        name = pdf_res.get("name")
        if name and isinstance(name, str) and name.lower().endswith(".pdf"):
            return name
        return segment or None

    def _parse_item(self, item: dict) -> dict | None:
        """Build a paper dict from a single package_search result item."""
        title = (item.get("title") or item.get("name") or "").strip()
        if not title:
            return None

        abstract = (item.get("notes") or "").strip()
        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] Short abstract ({len(abstract)}) for '{title[:60]}', skipping.")
            return None

        native_id = item.get("id") or ""
        name = item.get("name") or native_id
        url = self._DATASET_URL.format(name=name)

        resources = item.get("resources") or []
        pdf_res = self._pick_pdf_resource(resources)
        pdf_url = pdf_res.get("url") if pdf_res else None
        original_filename = self._original_filename(pdf_res) if pdf_res else None

        published_date = self._iso_date(item.get("metadata_created"))
        listed_date_raw = item.get("metadata_modified")
        listed_date = self._iso_date(listed_date_raw)

        org = item.get("organization") or {}
        publisher = org.get("title") or org.get("name") or ""
        authors = item.get("author") or publisher

        tags = [t.get("name") for t in (item.get("tags") or []) if t.get("name")]
        keywords = ", ".join(tags)

        groups = item.get("groups") or []
        group_names = [g.get("title") or g.get("name") for g in groups if isinstance(g, dict)]
        category = ", ".join(n for n in group_names if n)

        # post_number: native slug has no digits on this site; fall back to it,
        # or the raw dataset UUID when even the slug is missing.
        post_number = name or native_id or None

        meta = {
            "posted_date": listed_date_raw,
            "originalFilename": original_filename,
            "dataset_id": native_id,
            "dataset_name": name,
            "license_id": item.get("license_id"),
            "license_title": item.get("license_title"),
            "num_resources": item.get("num_resources"),
            "resource_formats": sorted({
                (r.get("format") or "").strip().upper()
                for r in resources if r.get("format")
            }),
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": native_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": None,
            "journal": None,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl data.gov.au datasets that expose a PDF resource."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        start = (self.delivery_cursor or {}).get("offset", 0)
        page_num = 0

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_num += 1
                items = self._fetch_page(start)
                if not items:
                    print(f"[{self.site_id}] No more results at start={start}. Stopping.")
                    break

                new_on_page = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    name = item.get("name") or item.get("id") or ""
                    url = self._DATASET_URL.format(name=name)
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    try:
                        time.sleep(self._delay)
                        paper = self._parse_item(item)
                        if paper is None:
                            continue

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {paper['title'][:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item failed (name={name}): {exc}; continuing.")
                        continue

                if page_num % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

                self._advance_cursor({"offset": start + self._ROWS_PER_PAGE}, items_done=len(items))

                if new_on_page == 0:
                    print(f"[{self.site_id}] Page {page_num} yielded 0 new records. Stopping.")
                    break

                start += self._ROWS_PER_PAGE

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
