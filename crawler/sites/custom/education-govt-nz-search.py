# -*- coding: utf-8 -*-
"""Crawler for education.govt.nz Corporate publications search.

Uses Algolia search API (reverse-engineered from browser XHR).
Index: date_desc_live, filter: result.lvl1 = Documents > Corporate publication
"""

import os
import json
import re
import sys
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import unquote

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_ALGOLIA_APP_ID = "ZJYLA4J4R6"
_ALGOLIA_API_KEY = os.environ.get("EDUCATION_GOVT_NZ_SEARCH_KEY", "")
_ALGOLIA_INDEX = "date_desc_live"
_ALGOLIA_URL = "https://zjyla4j4r6-dsn.algolia.net/1/indexes/*/queries"
_HITS_PER_PAGE = 20


class EducationGovtNzSearchCrawler(BaseCrawler):
    site_id = "education-govt-nz-search"
    site_name = "Custom: education-govt-nz-search"
    base_url = "https://www.education.govt.nz"

    def _algolia_query(self, page: int) -> Optional[dict]:
        """POST a single-request Algolia query and return the parsed JSON."""
        payload = {
            "requests": [
                {
                    "indexName": _ALGOLIA_INDEX,
                    "analytics": False,
                    "clickAnalytics": False,
                    "facetFilters": [
                        ["result.lvl1:Documents > Corporate publication"]
                    ],
                    "facetingAfterDistinct": True,
                    "filters": "status:true",
                    "hitsPerPage": _HITS_PER_PAGE,
                    "page": page,
                    "query": "",
                }
            ]
        }
        headers = {
            "x-algolia-application-id": _ALGOLIA_APP_ID,
            "x-algolia-api-key": _ALGOLIA_API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        for attempt in range(3):
            wait = [1, 3, 9][attempt]
            try:
                resp = self._request(
                    _ALGOLIA_URL,
                    method="POST",
                    json=payload,
                    headers=headers,
                )
                if resp is None:
                    raise RuntimeError("_request returned None")
                data = resp.json()
                if "results" not in data:
                    raise ValueError(f"Unexpected Algolia response: {str(data)[:200]}")
                return data
            except Exception as exc:
                print(
                    f"[{self.site_id}] Algolia page={page} attempt={attempt+1}/3 error: {exc}"
                )
                if attempt < 2:
                    time.sleep(wait)
        return None

    def _parse_date(self, created_ms) -> Optional[str]:
        if not created_ms:
            return None
        try:
            dt = datetime.fromtimestamp(int(created_ms) / 1000, tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except Exception:
            return None

    def _extract_filename(self, url: str) -> Optional[str]:
        if not url:
            return None
        try:
            tail = url.split("?")[0].rstrip("/").split("/")[-1]
            tail = unquote(tail)
            if "." in tail and len(tail) <= 200:
                return tail
        except Exception:
            pass
        return None

    def _build_abstract(self, hit: dict) -> str:
        """Build abstract from Algolia content + supplementary metadata.

        Algolia content fields are often short (<100 chars), so we enrich
        with title, document_type, publication date, and objectID to ensure
        the stored abstract is always meaningful and >= 100 chars.
        """
        content = hit.get("content", "") or ""
        content = content.strip()
        title = (hit.get("title") or "").strip()
        doc_type = (hit.get("document_type") or "").strip()

        parts = []
        if content:
            parts.append(content)
        if title and title not in content:
            parts.append(f"Title: {title}")
        if doc_type:
            parts.append(f"Document type: {doc_type}")

        created_ms = hit.get("created")
        if created_ms:
            date_str = self._parse_date(created_ms)
            if date_str:
                parts.append(f"Published: {date_str}")

        obj_id = hit.get("objectID", "") or ""
        if obj_id:
            parts.append(f"ID: {obj_id}")

        return ". ".join(p for p in parts if p)

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECONDS = 25 * 60

        # Fetch page 0 first to get pagination metadata.
        first = self._algolia_query(0)
        if not first:
            print(f"[{self.site_id}] Failed to fetch first page.")
            return saved

        res0 = first["results"][0]
        nb_pages = int(res0.get("nbPages") or 1)
        nb_hits = int(res0.get("nbHits") or 0)
        print(
            f"[{self.site_id}] Total: {nb_hits} hits across {nb_pages} pages."
        )

        pages_to_fetch = min(nb_pages, MAX_PAGES)

        for p in range(pages_to_fetch):
            # Time budget.
            if time.time() - start_time > MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget reached at page {p}, stopping.")
                break

            # Limit reached.
            if limit is not None and saved >= limit:
                break

            # Progress every 10 pages.
            if p > 0 and p % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(
                    f"[{self.site_id}] page {p}: saved {saved}/{limit_str}"
                )

            # Fetch page (page 0 already in hand).
            if p == 0:
                data = first
            else:
                data = self._algolia_query(p)
                if data is None:
                    print(f"[{self.site_id}] page {p}: fetch failed, skipping.")
                    continue

            hits = data["results"][0].get("hits") or []
            if not hits:
                print(f"[{self.site_id}] page {p}: empty hit list, pagination end.")
                break

            for hit in hits:
                if limit is not None and saved >= limit:
                    break

                try:
                    obj_id = hit.get("objectID") or ""
                    title = (hit.get("title") or "").strip()
                    file_url = (hit.get("file_url") or "").strip()

                    if not title:
                        print(
                            f"[{self.site_id}] skipping hit with no title: {obj_id}"
                        )
                        continue

                    # URL-based dedup (use objectID as fallback key).
                    dedup_key = file_url or obj_id
                    if dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    # Build and validate abstract.
                    abstract = self._build_abstract(hit)
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] skipping '{title[:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Dates.
                    created_ms = hit.get("created")
                    pub_date = self._parse_date(created_ms)

                    # post_number: numeric entity ID from objectID.
                    post_number = None
                    m = re.search(r"/(\d+):", obj_id)
                    if m:
                        post_number = m.group(1)

                    # URL: use file_url as the canonical document URL;
                    # paragraph entities have no separate HTML detail page
                    # exposed in the search API response.
                    url = file_url or (
                        f"{self.base_url}/search?site_search_live"
                        "%5BhierarchicalMenu%5D%5Bresult.lvl0%5D%5B0%5D=Documents"
                    )

                    original_filename = self._extract_filename(file_url)

                    metadata = {
                        "objectID": obj_id,
                        "search_api_id": hit.get("search_api_id"),
                        "entity_type": hit.get("entity_type"),
                        "file_type": hit.get("file_type"),
                        "file_size": hit.get("file_size"),
                        "result_type": hit.get("result_type"),
                        "result": hit.get("result"),
                        "algolia_index": _ALGOLIA_INDEX,
                    }

                    self._save_paper(
                        {
                            "site_id": self.site_id,
                            "external_id": obj_id,
                            "post_number": post_number,
                            "title": title,
                            "abstract": abstract,
                            "published_date": pub_date,
                            "listed_date": pub_date,
                            "url": url,
                            "pdf_url": file_url or None,
                            "category": hit.get("document_type") or "",
                            "keywords": None,
                            "authors": None,
                            "publisher": "Ministry of Education",
                            "department": None,
                            "journal": None,
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }
                    )
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {hit.get('objectID', '?')} failed: {exc}"
                    )
                    continue

                time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Saved: {saved}")
        return saved
