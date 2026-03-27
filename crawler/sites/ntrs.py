# -*- coding: utf-8 -*-
"""NASA NTRS (NASA Technical Reports Server) crawler."""

import json

from ..base_crawler import BaseCrawler


class NTRSCrawler(BaseCrawler):
    """Crawler for the NASA Technical Reports Server API."""

    site_id = "ntrs"
    site_name = "NASA NTRS"
    base_url = "https://ntrs.nasa.gov"

    _API_URL = "https://ntrs.nasa.gov/api/citations/search"
    _PAGE_SIZE = 25

    def crawl(self, limit=None):
        """Crawl NASA NTRS and save papers to the database."""
        offset = 0
        total = None
        saved = 0

        while True:
            # Stop if we have already reached the requested limit
            if limit is not None and saved >= limit:
                break

            # NTRS API requires POST with JSON body for pagination
            body = {"page": {"size": self._PAGE_SIZE, "from": offset}}
            response = self._request(
                self._API_URL, method="POST", json=body
            )
            if response is None:
                print(f"[{self.site_id}] Failed to fetch page at offset {offset}. Stopping.")
                break

            data = response.json()

            # Capture total on first request
            if total is None:
                total = data.get("stats", {}).get("total", 0)
                print(f"[{self.site_id}] Total papers available: {total}")

            results = data.get("results", [])
            if not results:
                break

            for item in results:
                if limit is not None and saved >= limit:
                    break

                paper = self._parse_item(item)
                self._save_paper(paper)
                saved += 1

            effective_total = total if limit is None else min(total, limit)
            print(f"[{self.site_id}] Crawled {saved}/{effective_total} papers...")

            offset += self._PAGE_SIZE
            if offset >= total:
                break

        print(f"[{self.site_id}] Done. Saved {saved} papers.")
        return saved

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_item(item: dict) -> dict:
        """Extract a normalised paper dict from a single API result."""
        item_id = str(item.get("id", ""))

        # Authors
        author_names = []
        for affil in item.get("authorAffiliations", []):
            name = (
                affil.get("meta", {})
                .get("author", {})
                .get("name", "")
            )
            if name:
                author_names.append(name)

        # Publications block
        pubs = item.get("publications", [])
        pub = pubs[0] if pubs else {}
        published_date = pub.get("publicationDate", "")
        doi = pub.get("doi", "")

        # PDF URL
        pdf_url = ""
        downloads = item.get("downloads", [])
        if downloads:
            pdf_url = downloads[0].get("links", {}).get("pdf", "")

        # Keywords
        keywords = item.get("keywords", [])

        return {
            "id": None,  # will be assigned by _save_paper
            "site_id": "ntrs",
            "external_id": item_id,
            "title": item.get("title", ""),
            "authors": json.dumps(author_names, ensure_ascii=False),
            "abstract": item.get("abstract", ""),
            "category": item.get("subjectCategories", [None])[0] if item.get("subjectCategories") else "",
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": f"https://ntrs.nasa.gov/citations/{item_id}" if item_id else "",
            "pdf_url": pdf_url,
            "doi": doi,
            "department": "",
            "metadata": json.dumps({
                "stiType": item.get("stiType", ""),
                "center": item.get("center", {}).get("code", "") if isinstance(item.get("center"), dict) else "",
            }, ensure_ascii=False),
        }
