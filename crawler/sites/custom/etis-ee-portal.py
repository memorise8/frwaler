# -*- coding: utf-8 -*-
"""Estonian Research Information System (ETIS) publications crawler.

Starting URL: https://www.etis.ee/Portal/Publications/Index/?

ETIS is a React SPA; the real data lives behind JSON endpoints under
``/services/Portal/Publications/...`` (discovered via Playwright network
capture, confirmed reachable with plain curl — no browser rendering
needed). ETIS itself stores no abstracts, so abstracts are enriched from
CrossRef (primary) and Semantic Scholar (fallback) via each record's DOI.
Records without a resolvable DOI/abstract are skipped.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_SEARCH_URL = "https://www.etis.ee/services/Portal/Publications/Search?lang=ENG"
_DISPLAY_API_TMPL = "https://www.etis.ee/services/Portal/Publications/Display/{id}?lang=ENG"
_DETAIL_PAGE_TMPL = "https://www.etis.ee/Portal/Publications/Display/{id}"
_CROSSREF_BASE = "https://api.crossref.org/works"
_S2_BASE = "https://api.semanticscholar.org/graph/v1/paper/DOI:"

_DOI_PREFIX_RE = re.compile(r"^https?://(dx\.)?doi\.org/", re.IGNORECASE)


def _strip_jats(text: str) -> str:
    """Remove JATS/XML tags and normalize whitespace."""
    text = re.sub(r"</?jats:[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _clean_doi(raw: str | None) -> str:
    if not raw:
        return ""
    return _DOI_PREFIX_RE.sub("", raw.strip()).strip("/ ")


class EtisEePortalCrawler(BaseCrawler):
    """Crawler for ETIS (Estonian Research Information System) publications."""

    site_id = "etis-ee-portal"
    site_name = "Custom: etis-ee-portal"
    base_url = "https://www.etis.ee"
    DELIVERY_ORDER = "arbitrary"

    _MIN_ABSTRACT = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _PAGE_SIZE = 100

    # ------------------------------------------------------------------
    # curl helpers (retry with exponential backoff: 1s, 3s, 9s)
    # ------------------------------------------------------------------

    def _curl(self, url: str, method: str = "GET", json_body: dict | None = None) -> str | None:
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
        ]
        if json_body is not None:
            cmd += ["-X", "POST", "-H", "Content-Type: application/json", "-d", json.dumps(json_body)]
        elif method != "GET":
            cmd += ["-X", method]
        cmd.append(url)

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
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}: {url}")
        return None

    # ------------------------------------------------------------------
    # List / detail fetchers
    # ------------------------------------------------------------------

    def _search_page(self, page: int) -> dict:
        """Fetch one page of the publications list. Returns parsed dict (may be empty)."""
        body = {
            "fdItems": [{"fdExpr": "AND", "fdField": "", "fdType": "1"}],
            "page": page,
            "pageSize": self._PAGE_SIZE,
            "cbGroups": [],
            "exports": ["GUID", "Short reference"],
            "keyword": "",
            "sortColumn": "",
            "sortDirection": 0,
            "cbSelected": [],
            "selectType": "3",
            "selectMax": "1000",
        }
        raw = self._curl(_SEARCH_URL, json_body=body)
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] search page {page} JSON parse error: {exc}")
            return {}

    def _fetch_detail(self, guid: str) -> dict:
        """Fetch and double-decode the ``Object`` field of a publication's detail API. Returns {} on failure."""
        raw = self._curl(_DISPLAY_API_TMPL.format(id=guid))
        if not raw:
            return {}
        try:
            outer = json.loads(raw)
            obj_raw = outer.get("Object")
            if not obj_raw:
                return {}
            return json.loads(obj_raw)
        except (json.JSONDecodeError, ValueError, AttributeError) as exc:
            print(f"[{self.site_id}] detail parse error for {guid}: {exc}")
            return {}

    # ------------------------------------------------------------------
    # Abstract enrichment (ETIS has no abstracts of its own)
    # ------------------------------------------------------------------

    def _fetch_crossref(self, doi: str) -> dict:
        raw = self._curl(f"{_CROSSREF_BASE}/{doi}")
        if not raw:
            return {}
        try:
            return json.loads(raw).get("message", {})
        except (json.JSONDecodeError, AttributeError, ValueError):
            return {}

    def _fetch_semanticscholar(self, doi: str) -> dict:
        raw = self._curl(f"{_S2_BASE}{doi}?fields=abstract")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) and not data.get("error") else {}
        except (json.JSONDecodeError, ValueError):
            return {}

    def _fetch_abstract(self, doi: str):
        """Return (abstract_text, source, crossref_message) trying CrossRef then Semantic Scholar."""
        cf = self._fetch_crossref(doi)
        abstract = _strip_jats(cf.get("abstract") or "")
        if len(abstract) >= self._MIN_ABSTRACT:
            return abstract, "crossref", cf

        s2 = self._fetch_semanticscholar(doi)
        abstract2 = (s2.get("abstract") or "").strip()
        if len(abstract2) >= self._MIN_ABSTRACT:
            return abstract2, "semanticscholar", cf

        return "", None, cf

    @staticmethod
    def _crossref_date(msg: dict) -> str:
        for key in ("published", "published-print", "published-online", "issued", "created"):
            dp = (msg.get(key) or {}).get("date-parts", [[]])
            if dp and dp[0]:
                parts = dp[0]
                if len(parts) >= 3:
                    return f"{parts[0]:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                if len(parts) == 2:
                    return f"{parts[0]:04d}-{int(parts[1]):02d}-01"
                if len(parts) == 1:
                    return f"{parts[0]:04d}-01-01"
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Walk the ETIS publications list, enriching abstracts via CrossRef/Semantic Scholar."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = (self.delivery_cursor or {}).get("page", 1)
        page_start = page
        lim_str = str(limit) if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                # _MAX_PAGES is a per-run chunk size (not an absolute ceiling) so a
                # resume from a large cursor still walks a full budget of pages this run.
                if page >= page_start + self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached this run. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                data = self._search_page(page)
                items = data.get("Items") or []
                if not items:
                    print(f"[{self.site_id}] page {page}: no items returned. Ending pagination.")
                    self._mark_exhausted()
                    break

                new_count = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    guid = item.get("Id")
                    if not guid:
                        continue
                    url = _DETAIL_PAGE_TMPL.format(id=guid)
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)
                    new_count += 1

                    try:
                        title = (item.get("Title") or "").strip()
                        if not title:
                            print(f"[{self.site_id}] {guid}: empty title, skipping.")
                            continue

                        obj = self._fetch_detail(guid)
                        if not obj:
                            print(f"[{self.site_id}] {guid}: no detail data, skipping.")
                            continue

                        doi = _clean_doi(obj.get("Doi"))
                        if not doi:
                            print(f"[{self.site_id}] {guid}: no DOI, skipping (no abstract source).")
                            continue

                        time.sleep(self._delay)
                        abstract, source, cf = self._fetch_abstract(doi)
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] {guid}: short/no abstract ({len(abstract)} chars), skipping.")
                            continue

                        year = obj.get("PublishingYear")
                        published_date = (
                            self._crossref_date(cf)
                            or (f"{year}-01-01" if year else None)
                        )
                        listed_date = f"{year}-01-01" if year else None

                        authors = "; ".join(
                            p.strip() for p in (obj.get("AuthorsText") or item.get("AuthorsText") or "").split(";")
                            if p.strip()
                        )

                        publisher = obj.get("PublishingHouseName") or cf.get("publisher") or ""
                        department = item.get("InstitutionNames") or ""
                        journal = obj.get("Periodical") or ""
                        keywords = obj.get("KeywordsAsFreeText") or ""
                        pub_type = obj.get("PublicationType") or item.get("PublicationTypeCode") or ""
                        classification = obj.get("ClassificationCode") or item.get("ClassificationCode") or ""
                        category = f"{pub_type} ({classification})" if classification else pub_type

                        meta = {
                            "posted_date": listed_date,
                            "originalFilename": None,
                            "journal_raw": journal or None,
                            "series": obj.get("Series"),
                            "volume": obj.get("Binding"),
                            "issue": obj.get("Number"),
                            "etis_id": guid,
                            "publication_type_code": item.get("PublicationTypeCode"),
                            "classification_code": classification or None,
                            "issn": obj.get("Issn"),
                            "isbn": obj.get("Isbn"),
                            "is_open_access": obj.get("IsOpenAccess"),
                            "institution_names": department or None,
                            "scopus_url": obj.get("ScopusUrl"),
                            "abstract_source": source,
                        }

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": guid,
                            "post_number": guid,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": authors,
                            "publisher": publisher,
                            "department": department,
                            "journal": journal,
                            "url": url,
                            "pdf_url": None,
                            "keywords": keywords,
                            "category": category,
                            "doi": doi,
                            "original_filename": None,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {guid} failed: {exc}; continuing.")
                        continue

                self._advance_cursor({"page": page + 1}, items_done=len(items))

                if new_count == 0:
                    print(f"[{self.site_id}] page {page}: all items already seen. Ending pagination.")
                    break

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
