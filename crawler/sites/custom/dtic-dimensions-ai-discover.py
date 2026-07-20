# -*- coding: utf-8 -*-
"""Dimensions.ai (DTIC edition) publication discovery crawler.

Starting URL: https://dtic.dimensions.ai/discover/publication

The "/discover/publication" page is an Angular SPA shell, but it fetches
its results from a plain JSON API at
``/discover/publication/results.json``. Each response contains a page of
20 ``docs`` plus a ``navigation.results_json`` cursor (``?np=<token>``)
pointing at the next page. Abstracts in the list response are truncated
to ~200 chars, so the full abstract is fetched per-item from
``/details/sources/publication/<id>/abstract.json``.
"""

from __future__ import annotations

import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class DticDimensionsAiDiscoverCrawler(BaseCrawler):
    """Crawler for dtic.dimensions.ai publication discovery."""

    site_id = "dtic-dimensions-ai-discover"
    site_name = "Custom: dtic-dimensions-ai-discover"
    base_url = "https://dtic.dimensions.ai"

    _LIST_URL = "https://dtic.dimensions.ai/discover/publication/results.json"
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/plain, */*",
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

    def _curl_get_json(self, url: str) -> dict | None:
        """GET a URL and parse it as JSON. Returns None on failure."""
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] JSON parse error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _iso_date(date_str) -> str | None:
        """Normalize a Dimensions date (YYYY, YYYY-MM, or YYYY-MM-DD) to ISO YYYY-MM-DD."""
        if not date_str:
            return None
        date_str = str(date_str).strip()
        parts = date_str.split("-")
        if len(parts) == 3 and all(parts):
            return date_str
        if len(parts) == 2 and all(parts):
            return f"{parts[0]}-{parts[1]}-01"
        if len(parts) == 1 and parts[0].isdigit():
            return f"{parts[0]}-01-01"
        return None

    @staticmethod
    def _post_number(pub_id: str) -> str | None:
        """Extract the numeric portion of a Dimensions id, e.g. 'pub.1200659973' -> '1200659973'."""
        if not pub_id:
            return None
        digits = re.sub(r"\D", "", pub_id)
        return digits or pub_id

    @staticmethod
    def _original_filename(pdf_url: str) -> str | None:
        if not pdf_url:
            return None
        tail = pdf_url.rstrip("/").rsplit("/", 1)[-1]
        tail = tail.split("?", 1)[0].split("#", 1)[0]
        return tail or None

    def _fetch_abstract(self, pub_id: str, abstract_path: str, short_abstract: str) -> str:
        """Fetch the full abstract text; falls back to the truncated short_abstract."""
        if abstract_path:
            url = self.base_url + abstract_path
            data = self._curl_get_json(url)
            if data:
                docs = data.get("docs") or []
                if docs and docs[0].get("abstract"):
                    return docs[0]["abstract"].strip()
        return (short_abstract or "").strip()

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Dimensions.ai (DTIC) publication discovery, paging via the `np` cursor."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page_num = 0
        next_url = self._LIST_URL
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while next_url:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                elapsed = time.time() - start_time
                if elapsed > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL}s) exceeded. Stopping cleanly.")
                    break

                page_num += 1
                data = self._curl_get_json(next_url)
                if not data:
                    print(f"[{self.site_id}] Failed to fetch page {page_num} ({next_url}). Stopping.")
                    break

                docs = data.get("docs") or []
                if not docs:
                    print(f"[{self.site_id}] page {page_num}: 0 records returned. Stopping.")
                    break

                new_on_page = 0
                for doc in docs:
                    if limit is not None and saved >= limit:
                        break

                    pub_id = doc.get("id") or ""
                    nav = doc.get("navigation") or {}
                    detail_path = nav.get("path") or (f"/details/publication/{pub_id}" if pub_id else None)
                    if not detail_path:
                        continue
                    url = self.base_url + detail_path
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_on_page += 1

                    try:
                        title = (doc.get("title") or "").strip()
                        if not title:
                            print(f"[{self.site_id}] Empty title for {pub_id}, skipping.")
                            continue

                        time.sleep(self._delay)
                        abstract = self._fetch_abstract(
                            pub_id, nav.get("abstract_json"), doc.get("short_abstract") or ""
                        )
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] Short abstract ({len(abstract)} chars) for "
                                f"'{title[:50]}', skipping."
                            )
                            continue

                        published_date = self._iso_date(doc.get("pub_date")) or self._iso_date(
                            doc.get("pub_year")
                        )
                        listed_date = self._iso_date(doc.get("print_pub_date")) or published_date

                        author_list = doc.get("author_list") or ""
                        authors = "; ".join(
                            p.strip() for p in author_list.split(",") if p.strip()
                        )

                        pdf_url = doc.get("linkout_oa") or None
                        doi = doc.get("doi") or None
                        journal = doc.get("journal_title") or doc.get("source_title") or ""
                        category = doc.get("pub_class") or ""

                        meta = {
                            "posted_date": listed_date,
                            "originalFilename": self._original_filename(pdf_url),
                            "journal_raw": doc.get("journal_title") or doc.get("source_title"),
                            "series": None,
                            "volume": doc.get("volume"),
                            "issue": None,
                            "dimensions_id": pub_id,
                            "doi_object": doi,
                            "pub_class_id": doc.get("pub_class_id"),
                            "source_title_id": doc.get("source_title_id"),
                            "issn": doc.get("issn"),
                            "pages": doc.get("pages"),
                            "times_cited": doc.get("times_cited"),
                            "pub_year": doc.get("pub_year"),
                            "print_pub_date": doc.get("print_pub_date"),
                            "abstract_original_length": doc.get("abstract_original_length"),
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": pub_id,
                            "post_number": self._post_number(pub_id),
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": authors,
                            "publisher": "",
                            "department": None,
                            "journal": journal,
                            "url": url,
                            "pdf_url": pdf_url,
                            "keywords": "",
                            "category": category,
                            "doi": doi,
                            "original_filename": self._original_filename(pdf_url),
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {pub_id or url} failed: {exc}; continuing.")
                        continue

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page_num}: all items already seen. Stopping.")
                    break

                nav_next = (data.get("navigation") or {}).get("results_json")
                if not nav_next:
                    print(f"[{self.site_id}] No next-page cursor found. Stopping.")
                    break
                next_url = self.base_url + nav_next

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
