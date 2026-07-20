# -*- coding: utf-8 -*-
"""iheid.swisscovery.ch — Primo VE journal-discovery crawler.

Uses the primaws REST API (JSON) for reliable machine access.
Endpoint: /primaws/rest/pub/pnxs
Total: ~5765 journal records.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


class IheidSwisscoveryChDiscoveryCrawler(BaseCrawler):
    site_id = "iheid-swisscovery-ch-discovery"
    site_name = "Custom: iheid-swisscovery-ch-discovery"
    base_url = "https://iheid.swisscovery.ch"

    _API_URL = "https://iheid.swisscovery.ch/primaws/rest/pub/pnxs"
    _PAGE_SIZE = 25
    _MAX_PAGES = 200
    _CRAWL_BUDGET_SECS = 25 * 60  # 25 minutes
    _RATE_SLEEP = 1.0
    _MIN_ABSTRACT = 100  # chars — skip items below this

    _BASE_PARAMS = {
        "inst": "41SLSP_IID",
        "lang": "en_US",
        "newsearch": "true",
        "pcAvailability": "false",
        "q": "any,contains,a",
        "rtaLinks": "true",
        "scope": "VU1_CUSTOM",
        "skipDelivery": "Y",
        "sort": "rank",
        "tab": "jsearch_slot",
        "vid": "41SLSP_IID:VU1_CUSTOM",
    }

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET url via curl; retry up to 3 times (1s / 3s / 9s backoff)."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=35)
                if res.stdout and res.stdout.strip():
                    return res.stdout.decode("utf-8", errors="replace")
                if attempt < 2:
                    wait = (1, 3, 9)[attempt]
                    print(f"[{self.site_id}] Empty response, retry in {wait}s…")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = (1, 3, 9)[attempt]
                    print(f"[{self.site_id}] curl error: {exc}, retry in {wait}s…")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Abstract construction
    # ------------------------------------------------------------------

    def _build_abstract(self, display: dict, addata: dict, control: dict) -> str:
        """Combine available metadata fields into a descriptive abstract."""
        def first(lst, default=""):
            return lst[0] if lst else default

        title = first(display.get("title", []))
        lang = first(display.get("language", []), "unknown")
        doc_type = first(display.get("type", []), "journal")

        # Resolve record ID (may be list or string)
        rid_raw = control.get("recordid", "")
        recordid = rid_raw[0] if isinstance(rid_raw, list) else str(rid_raw)

        issn_list = addata.get("issn", [])
        issn_str = ", ".join(issn_list) if issn_list else None
        publisher = first(display.get("publisher", []))
        coverage = first(display.get("coverage", []))
        freq = first(display.get("frequency", []))
        subjects = display.get("subject", [])
        genres = display.get("genre", [])
        place = first(display.get("place", []))
        creationdate = first(display.get("creationdate", []))

        # Core sentence
        t = title if title else recordid
        abstract = f'"{t}" is a {doc_type} published in {lang} language.'

        # Enrich with available fields
        _skip_publishers = {"[s.n.]", "[publisher not identified]", ""}
        if publisher and publisher not in _skip_publishers:
            abstract += f" Publisher: {publisher}."
        if issn_str:
            abstract += f" ISSN: {issn_str}."
        if creationdate:
            abstract += f" Date range: {creationdate}."
        if coverage:
            abstract += f" Coverage: {coverage}."
        if freq:
            abstract += f" Published {freq}."
        if subjects:
            abstract += " Subjects: " + "; ".join(subjects[:5]) + "."
        if genres:
            abstract += " Genre: " + ", ".join(genres[:3]) + "."
        if place and place.strip() not in (":", ""):
            abstract += f" Place: {place.strip()}."
        if recordid:
            abstract += f" Record ID: {recordid}."

        return abstract

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Primo VE journal records and save via _save_paper."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = limit if limit is not None else "unlimited"

        for page in range(self._MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > self._CRAWL_BUDGET_SECS:
                print(f"[{self.site_id}] 25-min budget reached at page {page}, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            if page == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")

            offset = page * self._PAGE_SIZE
            params = dict(self._BASE_PARAMS)
            params["limit"] = str(self._PAGE_SIZE)
            params["offset"] = str(offset)
            url = f"{self._API_URL}?{urlencode(params)}"

            raw = self._curl_get(url)
            if not raw:
                print(f"[{self.site_id}] No response at page {page}, stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON parse error page {page}: {exc}")
                break

            docs = data.get("docs", [])
            if not docs:
                print(f"[{self.site_id}] No more docs at offset {offset}, done.")
                break

            new_on_page = 0

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                try:
                    pnx = doc.get("pnx", {})
                    display = pnx.get("display", {})
                    addata = pnx.get("addata", {})
                    control = pnx.get("control", {})

                    # External ID
                    rid_raw = control.get("recordid", "")
                    external_id = (rid_raw[0] if isinstance(rid_raw, list)
                                   else str(rid_raw))
                    if not external_id:
                        continue

                    # Canonical URL
                    doc_url = (
                        f"{self.base_url}/discovery/fulldisplay"
                        f"?docid={external_id}"
                        f"&vid=41SLSP_IID:VU1_CUSTOM"
                    )

                    # Dedup
                    if doc_url in seen_urls:
                        continue
                    seen_urls.add(doc_url)
                    new_on_page += 1

                    # Title
                    title_list = display.get("title", [])
                    title = title_list[0] if title_list else external_id

                    # Date — prefer addata.date then display.creationdate
                    date_list = (addata.get("date") or
                                 display.get("creationdate") or [])
                    published_date = None
                    if date_list:
                        raw_date = date_list[0]
                        m = re.search(r"\d{4}", raw_date)
                        published_date = m.group(0) if m else raw_date[:10]

                    # Authors (rare for journals)
                    authors_list = (display.get("creator") or
                                    addata.get("au") or [])
                    authors = json.dumps(authors_list, ensure_ascii=False)

                    # Keywords
                    subjects = display.get("subject", [])
                    keywords = json.dumps(subjects, ensure_ascii=False)

                    # Category
                    type_list = display.get("type", ["journal"])
                    category = type_list[0] if type_list else "journal"

                    # Abstract
                    abstract = self._build_abstract(display, addata, control)
                    if len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] Skipping {external_id}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # DOI (rarely present for journals)
                    doi_list = addata.get("doi", [])
                    doi = doi_list[0] if doi_list else None

                    # Metadata object
                    def _first(lst, default=""):
                        return lst[0] if lst else default

                    issn_list = addata.get("issn", [])
                    meta = {
                        "type": category,
                        "issn": issn_list,
                        "source": _first(display.get("source", []), "Alma"),
                        "language": _first(display.get("language", [])),
                        "format": _first(display.get("format", [])),
                        "publisher": _first(display.get("publisher", [])),
                        "coverage": _first(display.get("coverage", [])),
                        "frequency": _first(display.get("frequency", [])),
                        "genre": display.get("genre", []),
                        "peer_reviewed": "true" in (addata.get("peerreview") or []),
                        "sourceformat": (
                            control["sourceformat"][0]
                            if isinstance(control.get("sourceformat"), list)
                            else control.get("sourceformat", "")
                        ),
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "title": title,
                        "authors": authors,
                        "abstract": abstract,
                        "category": category,
                        "keywords": keywords,
                        "published_date": published_date,
                        "url": doc_url,
                        "pdf_url": None,
                        "doi": doi,
                        "department": None,
                        "metadata": json.dumps(meta, ensure_ascii=False),
                    })
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    rid = (doc.get("pnx", {}).get("control", {})
                           .get("recordid", "?"))
                    print(f"[{self.site_id}] item {rid} failed: {exc}")
                    continue

            # If zero new items on this page, paginator has looped or ended
            if new_on_page == 0:
                print(
                    f"[{self.site_id}] All items on page {page} already seen, done."
                )
                break

            time.sleep(self._RATE_SLEEP)

        print(f"[{self.site_id}] Crawl complete: {saved} records saved.")
        return saved
