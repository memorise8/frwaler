# -*- coding: utf-8 -*-
"""Simula (simula.no) publications crawler.

Starting URL: https://www.simula.no/publications/?status=published&type=technical_reports

As of May 2026 the simula.no /publications page no longer lists items
itself -- it renders an "nvaMode" banner saying all Simula-affiliated
publications moved to NVA (Norwegian Research Information Repository,
https://nva.sikt.no), filtered by topLevelOrganization=7498.0.0.0 (Simula's
Cristin org id). The real data now lives behind NVA's public JSON API:

  - List:   GET https://api.nva.unit.no/search/resources
            (topLevelOrganization + type=, paginated via size/from)
  - Detail: GET https://api.nva.unit.no/publication/{identifier}
            (search hits don't reliably carry abstract/files/keywords --
            only the per-record detail endpoint does)

The requested simula.no "technical_reports" content type maps onto NVA's
report publication-instance types: ReportBasic and ReportResearch.
"""

from __future__ import annotations

import json
import os
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class SimulaNoPublicationsCrawler(BaseCrawler):
    """Crawler for Simula publications (technical reports), sourced from NVA."""

    site_id = "simula-no-publications"
    site_name = "Custom: simula-no-publications"
    base_url = "https://www.simula.no"

    _START_URL = "https://www.simula.no/publications/?status=published&type=technical_reports"
    _SEARCH_URL = "https://api.nva.unit.no/search/resources"
    _PUB_URL = "https://api.nva.unit.no/publication/{identifier}"
    _REG_URL = "https://nva.sikt.no/registration/{identifier}"
    _TOP_LEVEL_ORG = "7498.0.0.0"
    _REPORT_TYPES = "ReportResearch,ReportBasic"

    _PAGE_SIZE = 25
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _MIN_ABSTRACT = 50

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, headers: dict | None = None) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
        ]
        for key, value in (headers or {}).items():
            cmd += ["-H", f"{key}: {value}"]
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
                    print(f"[{self.site_id}] curl failed after 3 attempts: {url}: {exc}")
        return None

    def _curl_get_json(self, url: str, headers: dict | None = None) -> dict | None:
        raw = self._curl_get(url, headers=headers)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] JSON parse error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # NVA API helpers
    # ------------------------------------------------------------------

    def _search_page(self, from_: int) -> dict | None:
        url = (
            f"{self._SEARCH_URL}?topLevelOrganization={self._TOP_LEVEL_ORG}"
            f"&type={self._REPORT_TYPES}&size={self._PAGE_SIZE}&from={from_}"
            f"&sort=published_date&sortOrder=desc"
        )
        return self._curl_get_json(url, headers={"Accept": "application/json"})

    def _fetch_detail(self, identifier: str) -> dict | None:
        url = self._PUB_URL.format(identifier=identifier)
        return self._curl_get_json(url, headers={"Accept": "application/json"})

    # ------------------------------------------------------------------
    # Field extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _iso_date_from_parts(date_parts: dict) -> str | None:
        if not date_parts:
            return None
        year = date_parts.get("year")
        if not year:
            return None
        try:
            year_i = int(year)
        except (TypeError, ValueError):
            return None
        month = date_parts.get("month") or "1"
        day = date_parts.get("day") or "1"
        try:
            return f"{year_i:04d}-{int(month):02d}-{int(day):02d}"
        except (TypeError, ValueError):
            return f"{year_i:04d}-01-01"

    @staticmethod
    def _iso_date_from_timestamp(ts: str | None) -> str | None:
        if not ts or len(ts) < 10:
            return None
        return ts[:10]

    @staticmethod
    def _arxiv_pdf_url(doi: str | None) -> str | None:
        if not doi or "arXiv" not in doi:
            return None
        arxiv_id = doi.split("arXiv.")[-1].strip()
        if not arxiv_id:
            return None
        return f"https://arxiv.org/pdf/{arxiv_id}"

    def _extract_paper(self, hit: dict, detail: dict) -> dict | None:
        identifier = detail.get("identifier") or hit.get("identifier")
        if not identifier:
            return None

        ed = detail.get("entityDescription") or {}
        title = (ed.get("mainTitle") or "").strip()
        if not title:
            return None

        abstract = (ed.get("abstract") or "").strip()

        reference = ed.get("reference") or {}
        pub_context = reference.get("publicationContext") or {}
        pub_instance = reference.get("publicationInstance") or {}

        doi_raw = reference.get("doi")
        doi = None
        if doi_raw:
            doi = doi_raw.replace("https://doi.org/", "").strip() or None

        authors = "; ".join(
            c.get("identity", {}).get("name", "").strip()
            for c in (ed.get("contributors") or [])
            if c.get("identity", {}).get("name")
        ) or None

        # Publisher: prefer the name embedded in the detail record; the
        # detail record sometimes only carries a channel id (no "name"),
        # e.g. for registry-confirmed publishers like "Arxiv" -- in that
        # case fall back to the name the search hit already carried.
        publisher = (pub_context.get("publisher") or {}).get("name")
        if not publisher:
            hit_pub_context = (
                (hit.get("entityDescription") or {})
                .get("reference", {})
                .get("publicationContext", {})
            )
            publisher = (hit_pub_context.get("publisher") or {}).get("name")

        journal = None
        if (pub_context.get("type") or "") == "Journal":
            journal = publisher

        published_date = self._iso_date_from_parts(ed.get("publicationDate") or {})
        listed_date = (
            self._iso_date_from_timestamp(detail.get("publishedDate"))
            or self._iso_date_from_timestamp(detail.get("createdDate"))
        )

        keywords = ", ".join(ed.get("tags") or []) or None
        category = pub_instance.get("type") or None

        artifacts = detail.get("associatedArtifacts") or []
        open_files = [a for a in artifacts if a.get("type") == "OpenFile"]
        original_filename = open_files[0].get("name") if open_files else None

        # NVA's file-download API (api.nva.unit.no/download/public/...)
        # consistently returns 403 Forbidden for anonymous/non-browser
        # clients (verified via curl), so it is not usable here. Fall back
        # to a direct arXiv PDF link when the DOI identifies an arXiv
        # preprint, or to an explicit .pdf AssociatedLink if present.
        pdf_url = self._arxiv_pdf_url(doi_raw)
        if not pdf_url:
            for artifact in artifacts:
                link = artifact.get("id") or ""
                if artifact.get("type") == "AssociatedLink" and link.lower().endswith(".pdf"):
                    pdf_url = link
                    break

        cristin_id = None
        handle = None
        for extra in detail.get("additionalIdentifiers") or []:
            if extra.get("type") == "CristinIdentifier" and extra.get("value") and not cristin_id:
                cristin_id = str(extra["value"])
            elif extra.get("type") == "HandleIdentifier" and extra.get("value") and not handle:
                handle = extra["value"]

        post_number = cristin_id if (cristin_id and cristin_id.isdigit()) else identifier

        metadata = {
            "posted_date": detail.get("publishedDate") or detail.get("createdDate"),
            "originalFilename": original_filename,
            "journal_raw": None,
            "series": pub_context.get("seriesNumber"),
            "volume": None,
            "issue": None,
            "nva_identifier": identifier,
            "cristinIdentifier": cristin_id,
            "handle": handle,
            "topLevelOrganization": self._TOP_LEVEL_ORG,
            "publicationInstanceType": pub_instance.get("type"),
            "associatedArtifacts": [
                {"type": a.get("type"), "name": a.get("name"), "id": a.get("id")}
                for a in artifacts
            ],
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": identifier,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            # NOTE: crawler.libertree_adapter.paper_to_document() only reads
            # paper["posted_date"] (not "listed_date") to populate the
            # listed_date column downstream -- see BaseCrawler._save_paper's
            # v2_doc mapping ("listed_date": doc_dict.get("posted_date")).
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": None,
            "journal": journal,
            "url": self._REG_URL.format(identifier=identifier),
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Simula's technical-report publications via the NVA API."""
        print(f"[{self.site_id}] Starting from {self._START_URL}")
        print(f"[{self.site_id}] Simula publications migrated to NVA; pulling from {self._SEARCH_URL}")

        saved = 0
        seen_urls: set = set()
        start_time = time.time()

        try:
            for page in range(self._MAX_PAGES):
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL}s) exceeded. Stopping cleanly.")
                    break

                from_ = page * self._PAGE_SIZE
                data = self._search_page(from_)
                if data is None:
                    print(f"[{self.site_id}] Search page fetch failed at from={from_}. Stopping.")
                    break

                hits = data.get("hits") or []
                if not hits:
                    print(f"[{self.site_id}] page {page}: 0 records returned. End of pagination.")
                    break

                new_count = 0
                for hit in hits:
                    if limit is not None and saved >= limit:
                        break

                    identifier = hit.get("identifier")
                    if not identifier:
                        continue
                    item_url = self._REG_URL.format(identifier=identifier)
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)
                    new_count += 1

                    try:
                        time.sleep(self._delay)
                        detail = self._fetch_detail(identifier)
                        if detail is None:
                            print(f"[{self.site_id}] item {identifier} failed: detail fetch returned None; skipping.")
                            continue

                        paper = self._extract_paper(hit, detail)
                        if paper is None:
                            print(f"[{self.site_id}] item {identifier} failed: missing title/identifier; skipping.")
                            continue

                        if len(paper["abstract"]) < self._MIN_ABSTRACT:
                            print(
                                f"[{self.site_id}] item {identifier} skipped: "
                                f"abstract too short ({len(paper['abstract'])} chars)."
                            )
                            continue

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {paper['title'][:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {identifier} failed: {exc}; continuing.")
                        continue

                if new_count == 0:
                    print(f"[{self.site_id}] page {page}: all records already seen. Stopping.")
                    break

                if (page + 1) % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page + 1}: saved {saved}/{lim_str}")

                total_hits = data.get("totalHits")
                if isinstance(total_hits, int) and from_ + len(hits) >= total_hits:
                    print(f"[{self.site_id}] Reached end of result set ({total_hits} total).")
                    break
            else:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
