# -*- coding: utf-8 -*-
"""USDA NAL Collection Discovery crawler — NAL Publications Archive.

Target: https://search.nal.usda.gov/discovery/collectionDiscovery
        ?vid=01NAL_INST:MAIN&collectionId=81279629900007426

The site is Ex Libris Primo (Angular SPA).  Records are fetched via the
Primo pnxs REST API using the collection-discovery scope and a
cdparentid query:

  GET /primaws/rest/pub/pnxs
      ?inst=01NAL_INST
      &search_scope=collection_discovery_search
      &tab=Everything
      &q=cdparentid,exact,81279629900007426
      &isCDSearch=true
      ...

Abstracts are either empty (≈90%) or ≥100 chars (≈10%) — there is no
in-between cluster.  Items with abstract < 50 chars are skipped.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_API_URL = "https://search.nal.usda.gov/primaws/rest/pub/pnxs"
_VID = "01NAL_INST:MAIN"
_INST = "01NAL_INST"
_COLLECTION_ID = "81279629900007426"
_PAGE_SIZE = 50
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT = 50
_MAX_WALL_SEC = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
_REFERER = (
    "https://search.nal.usda.gov/discovery/collectionDiscovery"
    f"?vid={_VID}&collectionId={_COLLECTION_ID}"
)


class SearchNalUsdaGovDiscoveryCrawler(BaseCrawler):
    """Crawler for USDA NAL Publications Archive (Primo ExLibris)."""

    site_id = "search-nal-usda-gov-discovery"
    site_name = "Custom: search-nal-usda-gov-discovery"
    base_url = "https://search.nal.usda.gov"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _first(lst, default=""):
        if lst and isinstance(lst, list) and lst[0]:
            return lst[0]
        return default

    @staticmethod
    def _strip_alma(s):
        """Strip Alma $$X subfield markers."""
        return re.sub(r"\$\$[A-Z][^$]*", "", str(s or "")).strip()

    def _fetch_page(self, offset):
        """Fetch one page via curl.  Returns parsed JSON dict or None."""
        url = (
            f"{_API_URL}"
            f"?inst={_INST}"
            f"&lang=en"
            f"&limit={_PAGE_SIZE}"
            f"&offset={offset}"
            f"&q=cdparentid%2Cexact%2C{_COLLECTION_ID}"
            f"&search_scope=collection_discovery_search"
            f"&tab=Everything"
            f"&vid={_VID}"
            f"&skipDelivery=Y"
            f"&isCDSearch=true"
        )
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"Referer: {_REFERER}",
            "-H", "Accept: application/json",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if raw and raw.strip():
                    try:
                        return json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        pass
                if attempt < 2:
                    wait = (attempt + 1) ** 2
                    print(
                        f"[{self.site_id}] Bad/empty response at offset "
                        f"{offset}, retry {attempt + 1}/3 in {wait}s..."
                    )
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (attempt + 1) ** 2
                    print(
                        f"[{self.site_id}] curl error at offset {offset}: "
                        f"{exc}, retry {attempt + 1}/3 in {wait}s..."
                    )
                    time.sleep(wait)
                else:
                    print(
                        f"[{self.site_id}] curl failed after 3 attempts "
                        f"at offset {offset}: {exc}"
                    )
        return None

    def _parse_doc(self, doc):
        """Extract fields from a pnx doc dict.  Returns paper dict or None."""
        pnx = doc.get("pnx", {})
        display = pnx.get("display", {})
        addata = pnx.get("addata", {})
        control = pnx.get("control", {})

        recordid = self._first(control.get("recordid"))
        if not recordid:
            return None

        mms_id = self._first(control.get("sourcerecordid")) or recordid

        title = self._strip_alma(self._first(display.get("title"), ""))
        if not title:
            return None

        # Abstract: prefer addata.abstract, fallback to display.description
        abstract = self._first(addata.get("abstract"), "")
        if not abstract:
            abstract = self._first(display.get("description"), "")

        # Date → YYYY
        date_raw = self._first(
            addata.get("date"),
            self._first(display.get("creationdate"), ""),
        )
        published_date = ""
        if date_raw:
            yr = re.search(r"\b(\d{4})\b", str(date_raw))
            published_date = yr.group(1) if yr else str(date_raw)[:10]

        # Detail URL (Primo fulldisplay)
        detail_url = (
            f"https://search.nal.usda.gov/discovery/fulldisplay"
            f"?context=L&vid={_VID}&docid={recordid}"
        )

        # Resource/PDF URL (handle or archive.org link stored in addata.url)
        resource_urls = addata.get("url") or []
        pdf_url = resource_urls[0] if resource_urls else None

        # Original filename from PDF path segment
        original_filename = None
        if pdf_url:
            m = re.search(r"/([^/?#]+\.pdf)", pdf_url, re.I)
            if m:
                original_filename = m.group(1)

        # Authors (primary + additional), semicolon separated
        au_list = [self._strip_alma(a) for a in (addata.get("au") or [])]
        addau_list = [self._strip_alma(a) for a in (addata.get("addau") or [])]
        authors = "; ".join(a for a in au_list + addau_list if a) or None

        # Publisher
        pub_raw = display.get("publisher") or []
        publisher = "; ".join(pub_raw) if pub_raw else None

        # Journal / series
        series_raw = addata.get("seriestitle") or display.get("series") or []
        journal = self._strip_alma(self._first(series_raw, "")) or None

        # Keywords (comma-separated from display.subject)
        subjects = display.get("subject") or []
        keywords = ", ".join(s for s in subjects if s) or None

        # Category and DOI
        category = self._first(display.get("type")) or None
        doi_list = addata.get("doi") or []
        doi = doi_list[0] if doi_list else None

        metadata = json.dumps(
            {
                "posted_date": None,
                "originalFilename": original_filename,
                "journal_raw": self._strip_alma(
                    self._first(series_raw, "")
                ) or None,
                "series": self._strip_alma(
                    self._first(display.get("series") or [], "")
                ) or None,
                "mms_id": mms_id,
                "sourceid": control.get("sourceid"),
                "format": display.get("format"),
                "genre": display.get("genre"),
                "language": display.get("language"),
                "resource_urls": resource_urls or None,
                "collection_id": _COLLECTION_ID,
                "category": category,
                "doi": doi,
            },
            ensure_ascii=False,
        )

        return {
            "site_id": self.site_id,
            "external_id": recordid,
            "post_number": mms_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date or None,
            "listed_date": None,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "authors": authors,
            "publisher": publisher,
            "journal": journal,
            "department": None,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "metadata": metadata,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the NAL Publications Archive collection."""
        saved = 0
        seen_ids = set()
        offset = 0
        page = 0
        start_time = time.time()
        limit_disp = str(limit) if limit is not None else "∞"

        while True:
            # Wall-clock budget
            if time.time() - start_time > _MAX_WALL_SEC:
                print(
                    f"[{self.site_id}] 25-minute budget reached. "
                    "Exiting cleanly."
                )
                break

            # Safety page cap
            if page >= _MAX_PAGES:
                print(
                    f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages "
                    "reached. Stopping."
                )
                break

            if limit is not None and saved >= limit:
                break

            time.sleep(self._delay)

            data = self._fetch_page(offset)
            if data is None:
                print(
                    f"[{self.site_id}] Failed to fetch page {page + 1} "
                    f"(offset {offset}). Stopping."
                )
                break

            docs = data.get("docs", [])
            total = data.get("info", {}).get("total", 0)

            if page == 0:
                print(
                    f"[{self.site_id}] Collection {_COLLECTION_ID}: "
                    f"{total} total records."
                )

            if not docs:
                print(
                    f"[{self.site_id}] No docs at offset {offset}. Done."
                )
                break

            new_this_page = 0
            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                item_id = ""
                try:
                    pnx = doc.get("pnx", {})
                    ctrl = pnx.get("control", {})
                    item_id = self._first(ctrl.get("recordid")) or ""

                    if item_id in seen_ids:
                        continue
                    if item_id:
                        seen_ids.add(item_id)
                    new_this_page += 1

                    paper = self._parse_doc(doc)
                    if paper is None:
                        continue

                    abstract = paper.get("abstract", "")
                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Skipping "
                            f"'{paper['title'][:50]}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_disp}: "
                        f"{paper['title'][:60]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {item_id!r} failed: {exc}"
                    )
                    continue

            if page > 0 and page % 10 == 0:
                print(
                    f"[{self.site_id}] page {page}: "
                    f"saved {saved}/{limit_disp}"
                )

            # End-of-collection or infinite-loop detection
            if new_this_page == 0:
                print(
                    f"[{self.site_id}] No new records at offset {offset} "
                    "(all seen). Done."
                )
                break

            offset += len(docs)
            page += 1

            if total > 0 and offset >= total:
                print(
                    f"[{self.site_id}] Reached collection end "
                    f"({offset}/{total}). Done."
                )
                break

        print(f"[{self.site_id}] Finished. Total saved: {saved}")
        return saved
