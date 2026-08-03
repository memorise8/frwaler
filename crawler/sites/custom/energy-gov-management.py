# -*- coding: utf-8 -*-
"""U.S. Department of Energy — Directives, Guidance, and Delegations crawler.

Starting URL: https://www.energy.gov/management/directives-guidance-and-delegations

That landing page is just a hub of links; the actual records live in two
Drupal "datatable" views it links to:

  - /management/directives-library  — DOE Orders/Guides/Notices/Manuals,
    each row carrying a real description (``DESC``) and a ``URL`` that
    redirects (302) to a document alias page which streams the PDF
    directly (``Content-Type: application/pdf``).
  - /management/delegations-library — Delegation orders, no free-text
    description but a direct PDF link (``DIRECTLINK``) and filename
    (``PDFname``) already resolved — no redirect chain to follow.

Both tables are not paginated over the network: the full row set is
embedded as JSON inside a ``<script type="application/json"
data-drupal-selector="drupal-settings-json">`` tag on first load
(``drupalSettings.datatableRows``). We fetch each table page once, then
walk the combined row list in fixed-size batches so the page/progress
bookkeeping the rest of the crawler suite relies on still applies.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:  # pragma: no cover - bs4 always installed in this repo
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


class EnergyGovManagementCrawler(BaseCrawler):
    """Crawler for energy.gov's DOE directives/delegations libraries."""

    site_id = "energy-gov-management"
    site_name = "Custom: energy-gov-management"
    base_url = "https://www.energy.gov"

    _LANDING_URL = "https://www.energy.gov/management/directives-guidance-and-delegations"
    _DIRECTIVES_URL = "https://www.energy.gov/management/directives-library"
    _DELEGATIONS_URL = "https://www.energy.gov/management/delegations-library"
    _PUBLISHER = "U.S. Department of Energy"

    _MIN_ABSTRACT = 50
    _PAGE_SIZE = 10
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
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
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_resolve(self, url: str) -> tuple[str, str] | None:
        """Resolve redirects via ``curl -I -L``. Returns (final_url, content_type) or None."""
        cmd = [
            "curl", "-skI", "-L", "--tls-max", "1.3", "--max-time", "20",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=25)
                text = result.stdout.decode("utf-8", errors="replace")
                if not text.strip():
                    raise RuntimeError("empty header response")
                final_url = url
                content_type = ""
                for line in text.splitlines():
                    low = line.strip().lower()
                    if low.startswith("location:"):
                        final_url = line.split(":", 1)[1].strip()
                    elif low.startswith("content-type:"):
                        content_type = line.split(":", 1)[1].strip()
                return final_url, content_type
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] resolve error ({exc}), retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] resolve failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _extract_datatable_rows(self, raw_html: str, label: str) -> list:
        """Pull ``drupalSettings.datatableRows`` out of the embedded JSON script tag."""
        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for {label}: {exc}")
            return []

        tag = soup.find("script", attrs={"data-drupal-selector": "drupal-settings-json"})
        if tag is None or not tag.string:
            print(f"[{self.site_id}] no drupal-settings-json script found for {label}")
            return []

        try:
            settings = json.loads(tag.string)
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"[{self.site_id}] settings JSON parse error for {label}: {exc}")
            return []

        rows = settings.get("datatableRows")
        if not isinstance(rows, list):
            print(f"[{self.site_id}] no datatableRows found for {label}")
            return []
        return rows

    @staticmethod
    def _parse_date(raw: str) -> str | None:
        raw = (raw or "").strip()
        if not raw:
            return None
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                continue
        return None

    @staticmethod
    def _media_id(url: str) -> str | None:
        match = re.search(r"/media/(\d+)", url or "")
        return match.group(1) if match else None

    @staticmethod
    def _last_segment(url: str) -> str:
        return (url or "").rstrip("/").rsplit("/", 1)[-1]

    def _fetch_directives(self) -> list:
        raw = self._curl_get(self._DIRECTIVES_URL)
        if not raw:
            print(f"[{self.site_id}] failed to fetch directives library")
            return []
        rows = self._extract_datatable_rows(raw, "directives-library")
        for row in rows:
            row["_source"] = "directives"
        print(f"[{self.site_id}] directives-library: {len(rows)} rows")
        return rows

    def _fetch_delegations(self) -> list:
        raw = self._curl_get(self._DELEGATIONS_URL)
        if not raw:
            print(f"[{self.site_id}] failed to fetch delegations library")
            return []
        rows = self._extract_datatable_rows(raw, "delegations-library")
        for row in rows:
            row["_source"] = "delegations"
        print(f"[{self.site_id}] delegations-library: {len(rows)} rows")
        return rows

    # ------------------------------------------------------------------
    # Item builders
    # ------------------------------------------------------------------

    def _build_directive_item(self, row: dict) -> dict | None:
        media_url = (row.get("URL") or "").strip()
        if not media_url:
            return None
        title = (row.get("TITLE") or "").strip()
        if not title:
            return None
        abstract = (row.get("DESC") or "").strip()
        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] short abstract ({len(abstract)}) for '{title[:50]}', skipping")
            return None

        pdf_url = media_url
        original_filename = None
        time.sleep(self._delay)
        resolved = self._curl_resolve(media_url)
        if resolved:
            final_url, content_type = resolved
            if "pdf" in content_type.lower() or final_url != media_url:
                pdf_url = final_url
                original_filename = f"{self._last_segment(final_url)}.pdf"
        else:
            print(f"[{self.site_id}] could not resolve {media_url}, keeping media URL as pdf_url")

        edate = row.get("EDATE") or ""
        iso_date = self._parse_date(edate)
        native_id = (row.get("ID") or "").strip() or None
        post_number = self._media_id(media_url) or native_id

        areas = (row.get("AREAS") or "").strip()
        keywords = ", ".join(a.strip() for a in areas.split(";") if a.strip()) if areas else ""

        metadata = {
            "posted_date": edate,
            "originalFilename": original_filename,
            "series": row.get("SERIES") or "",
            "areas": areas,
            "doe_id": native_id,
            "doc_type": row.get("TYPE") or "",
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": native_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": iso_date,
            "listed_date": iso_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": row.get("ORG") or None,
            "journal": None,
            "url": media_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": row.get("TYPE") or "",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _build_delegation_item(self, row: dict) -> dict | None:
        pdf_url = (row.get("DIRECTLINK") or "").strip()
        if not pdf_url:
            return None
        title = (row.get("TITLE") or "").strip()
        if not title:
            return None

        delegant_pos = (row.get("DELEGANTPOSITION") or "").strip()
        delegate_pos = (row.get("DELEGATEPOSITION") or "").strip()
        parts = [title]
        if delegant_pos:
            parts.append(f"Delegant: {delegant_pos} ({row.get('DELEGANT') or ''}).")
        if delegate_pos:
            parts.append(f"Delegate: {delegate_pos} ({row.get('DELEGATE') or ''}).")
        abstract = " ".join(p for p in parts if p).strip()
        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] short abstract ({len(abstract)}) for '{title[:50]}', skipping")
            return None

        edate = row.get("EDATE") or ""
        iso_date = self._parse_date(edate)
        native_id = (row.get("ID") or "").strip() or None
        # native_id (e.g. "S1-DEL-RATES-1993") has no genuine sequential
        # digits of its own — the trailing number is just a year, not a
        # counter — so use the slug as-is rather than extracting it.
        post_number = native_id
        original_filename = (row.get("PDFname") or "").strip() or f"{self._last_segment(pdf_url)}"

        metadata = {
            "posted_date": edate,
            "originalFilename": original_filename,
            "delegant": row.get("DELEGANT") or "",
            "delegant_position": delegant_pos,
            "delegate": row.get("DELEGATE") or "",
            "delegate_position": delegate_pos,
            "doe_id": native_id,
            "doc_type": row.get("TYPE") or "",
        }

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": native_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": iso_date,
            "listed_date": iso_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": delegant_pos or None,
            "journal": None,
            "url": pdf_url,
            "pdf_url": pdf_url,
            "keywords": "",
            "category": row.get("TYPE") or "Delegation",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl DOE directives + delegations libraries (single-shot embedded tables)."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()

        # Touch the landing page first, purely to confirm the hub is alive
        # and to discover the sub-page links (kept for observability/debug).
        landing = self._curl_get(self._LANDING_URL)
        if not landing:
            print(f"[{self.site_id}] landing page unreachable; continuing to library pages anyway")

        directive_rows = self._fetch_directives()
        delegation_rows = self._fetch_delegations()
        combined = directive_rows + delegation_rows

        if not combined:
            print(f"[{self.site_id}] No rows found from either library. Aborting.")
            return 0

        lim_str = str(limit) if limit is not None else "inf"
        pages = [combined[i:i + self._PAGE_SIZE] for i in range(0, len(combined), self._PAGE_SIZE)]

        try:
            for page_num, page_rows in enumerate(pages, start=1):
                if limit is not None and saved >= limit:
                    break
                if page_num > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                for row in page_rows:
                    if limit is not None and saved >= limit:
                        break

                    source = row.get("_source")
                    dedup_key = row.get("DIRECTLINK") or row.get("URL") or row.get("ID")
                    if not dedup_key or dedup_key in seen_urls:
                        continue
                    seen_urls.add(dedup_key)

                    try:
                        if source == "directives":
                            item = self._build_directive_item(row)
                        elif source == "delegations":
                            item = self._build_delegation_item(row)
                        else:
                            item = None

                        if item is None:
                            continue

                        self._save_paper(item)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {item['title'][:60]}")
                    except Exception as exc:
                        row_id = row.get("ID", "?")
                        print(f"[{self.site_id}] item failed (id={row_id}): {exc}; continuing.")
                        continue

                if page_num % 10 == 0:
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
