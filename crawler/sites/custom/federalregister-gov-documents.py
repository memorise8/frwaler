# -*- coding: utf-8 -*-
"""Federal Register — HHS documents crawler (public JSON API)."""

import json
import os
import sys
import time

# Absolute import — spec_from_file_location has no package context
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler


class FederalRegisterDocsCrawler(BaseCrawler):
    """Crawler for Federal Register HHS documents via the public JSON API.

    Endpoint: https://www.federalregister.gov/api/v1/documents
    Filtered to: Health and Human Services Department, search_type_id=3, order=newest
    """

    site_id = "federalregister-gov-documents"
    site_name = "Custom: federalregister-gov-documents"
    base_url = "https://www.federalregister.gov"

    _API_BASE = "https://www.federalregister.gov/api/v1/documents"
    _AGENCY_SLUG = "health-and-human-services-department"
    _PAGE_SIZE = 200  # FR API supports up to 1000; 200 is safe and fast

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                # Exit: limit reached
                if limit is not None and saved >= limit:
                    break

                # Safety cap: 200 pages
                if page > 200:
                    print(f"[{self.site_id}] Safety cap of 200 pages reached. Stopping.")
                    break

                # Wall-clock budget: 25 minutes
                if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                    print(f"[{self.site_id}] 25-minute wall-clock budget reached. Stopping.")
                    break

                # Never fetch more items than needed
                per_page = self._PAGE_SIZE
                if limit is not None:
                    per_page = min(per_page, limit - saved)
                    if per_page <= 0:
                        break

                # Build params as list of tuples (handles bracket-style keys correctly)
                params = [
                    ("conditions[agencies][]", self._AGENCY_SLUG),
                    ("conditions[search_type_id]", "3"),
                    ("order", "newest"),
                    ("per_page", str(per_page)),
                    ("page", str(page)),
                ]

                resp = self._request(self._API_BASE, params=params)
                if resp is None:
                    print(f"[{self.site_id}] Failed to fetch page {page} after retries. Stopping.")
                    break

                try:
                    data = resp.json()
                except Exception as exc:
                    print(f"[{self.site_id}] JSON parse error at page {page}: {exc}. Stopping.")
                    break

                results = data.get("results") or []
                if not results:
                    print(f"[{self.site_id}] No results at page {page}. Done.")
                    break

                total_pages = int(data.get("total_pages") or 1)
                if page == 1:
                    total_count = data.get("count", "?")
                    print(f"[{self.site_id}] Total documents on server: {total_count}, pages: {total_pages}")

                page_new = 0
                for item in results:
                    if limit is not None and saved >= limit:
                        break

                    try:
                        html_url = (item.get("html_url") or "").strip()
                        doc_number = (item.get("document_number") or "").strip()

                        # URL deduplication — prevents infinite loops if paginator loops
                        if html_url:
                            if html_url in seen_urls:
                                continue
                            seen_urls.add(html_url)

                        title = (item.get("title") or "").strip()
                        if not title:
                            print(f"[{self.site_id}] Skipping item with no title ({doc_number})")
                            continue

                        # Abstract: prefer full abstract, fall back to excerpts
                        abstract = (item.get("abstract") or "").strip()
                        if len(abstract) < 50:
                            fallback = (item.get("excerpts") or "").strip()
                            if len(fallback) > len(abstract):
                                abstract = fallback

                        if len(abstract) < 50:
                            print(f"[{self.site_id}] Skipping {doc_number}: abstract too short ({len(abstract)} chars)")
                            continue

                        # Supplement to guarantee >= 100 chars (edge case: FR items
                        # with very short abstracts like single-sentence notices)
                        if len(abstract) < 100:
                            doc_type_str = (item.get("type") or "").strip()
                            agencies_raw = item.get("agencies") or []
                            anames = [a.get("name", "") for a in agencies_raw if a.get("name")]
                            supplement = (
                                f" [Document type: {doc_type_str}."
                                f" Published by: {'; '.join(anames)}."
                                f" Document number: {doc_number}.]"
                            )
                            abstract = abstract + supplement

                        pub_date = (item.get("publication_date") or "").strip()
                        pdf_url = (item.get("pdf_url") or "").strip() or None
                        doc_type = (item.get("type") or "").strip()

                        # Publisher: join all agency names with ";"
                        agencies = item.get("agencies") or []
                        agency_names = [a.get("name", "") for a in agencies if a.get("name")]
                        publisher = "; ".join(agency_names) if agency_names else None

                        # Sub-agencies (parent_id != None) collected for metadata
                        sub_agencies = [
                            a.get("name", "")
                            for a in agencies
                            if a.get("name") and a.get("parent_id") is not None
                        ]

                        # Topics as keywords
                        topics = item.get("topics") or []
                        keywords = ", ".join(str(t) for t in topics) if topics else None

                        # original_filename from the last path segment of pdf_url
                        original_filename = None
                        if pdf_url:
                            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                            if "." in tail and len(tail) <= 200:
                                original_filename = tail

                        paper = {
                            "site_id": self.site_id,
                            "external_id": doc_number,
                            "post_number": doc_number,
                            "url": html_url,
                            "title": title,
                            "abstract": abstract,
                            "published_date": pub_date,
                            "posted_date": pub_date,
                            "listed_date": pub_date,
                            "authors": None,
                            "publisher": publisher,
                            "pdf_url": pdf_url,
                            "keywords": keywords,
                            "category": doc_type,
                            "original_filename": original_filename,
                            "doi": None,
                            "metadata": json.dumps({
                                "posted_date": pub_date,
                                "document_number": doc_number,
                                "document_type": doc_type,
                                "sub_agencies": sub_agencies,
                                "agency_slugs": [a.get("slug", "") for a in agencies],
                                "public_inspection_pdf_url": item.get("public_inspection_pdf_url"),
                            }, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        page_new += 1
                        print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {doc_number!r} failed: {exc}")
                        continue

                # Progress log every 10 pages
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                # All items on this page were already seen → infinite loop guard
                if page_new == 0 and len(results) > 0:
                    print(f"[{self.site_id}] Page {page}: all items already seen. Stopping.")
                    break

                # End-of-pagination
                if page >= total_pages:
                    print(f"[{self.site_id}] Reached last page ({page}/{total_pages}). Done.")
                    break

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved so far: {saved}")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
