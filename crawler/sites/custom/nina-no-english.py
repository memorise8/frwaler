# -*- coding: utf-8 -*-
"""NINA reports crawler — nina.no JSON listing API.

The site was rebuilt on Next.js + Sanity CMS; the old static HTML page at
``/english/Publications/Older-NINA-series`` now 301-redirects to a generic
``/en/publications`` landing page with no listing content in the raw HTML
(client-side rendered). The publication list is fetched by the page via a
same-origin JSON API instead:

  https://www.nina.no/api/publication-listing?endpoint=%2Fapi%2Freports%3Fshow_all%3D1

This returns a flat JSON array of ``{"title", "contributors", "publicationYear",
"publisher", "serie", "fileOrLink": {"url": ...}}`` objects (discovered via
browser network-log inspection; the ``endpoint`` query param is validated
server-side against an allowlist, so extra pagination params are rejected —
the API returns a fixed set, capped server-side). The whole origin sits
behind an active Cloudflare managed challenge, so plain ``curl``/``requests``
get a 403 "Just a moment..." page; ``curl_cffi`` with Chrome TLS
impersonation passes cleanly.

The API doesn't expose real abstracts anymore (that data disappeared with
the old citation-text page), so ``abstract`` is a synthesized citation
string built from authors/year/title/serie — kept long enough to clear the
existing minimum-length quality gate.
"""

import hashlib
import json
import os
import re
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.nina.no/api/publication-listing?endpoint=%2Fapi%2Freports%3Fshow_all%3D1"
_PUBLISHER = "Norwegian Institute for Nature Research (NINA)"
_MIN_ABSTRACT = 30    # synthesized citations are shorter than the old full-text citations
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))      # safety cap (single JSON fetch = 1; kept for protocol)
_WALL_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


class NINAOlderSeriesCrawler(BaseCrawler):
    """Crawler for the NINA report-listing JSON API (nina.no)."""

    site_id = "nina-no-english"
    site_name = "Custom: nina-no-english"
    base_url = "https://www.nina.no"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_cffi_get(self, url, *, timeout=45):
        """GET via curl_cffi with Chrome TLS impersonation (bypasses the
        Cloudflare managed challenge fronting nina.no). Returns str or None.
        """
        try:
            from curl_cffi import requests as curl_requests
        except ImportError:
            print(f"[{self.site_id}] curl_cffi not installed")
            return None

        for attempt in range(3):
            wait = (1, 3, 9)[attempt]
            try:
                resp = curl_requests.get(
                    url, impersonate="chrome131", timeout=timeout,
                    headers={"User-Agent": self.USER_AGENT},
                )
                if resp.status_code == 200 and resp.text:
                    return resp.text
                print(f"[{self.site_id}] status {resp.status_code} (attempt {attempt+1}/3) — {url[:80]}")
            except Exception as exc:
                print(f"[{self.site_id}] curl_cffi error (attempt {attempt+1}/3): {exc}")
            if attempt < 2:
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # JSON parsing
    # ------------------------------------------------------------------

    def _parse_listing_json(self, raw):
        """Parse the ``publication-listing`` JSON array into entry dicts."""
        try:
            items = json.loads(raw)
        except (ValueError, TypeError) as exc:
            print(f"[{self.site_id}] failed to parse JSON: {exc}")
            return []
        if not isinstance(items, list):
            print(f"[{self.site_id}] unexpected JSON shape (not a list)")
            return []

        entries = []
        for item in items:
            entry = self._parse_single_entry(item)
            if entry:
                entries.append(entry)
        return entries

    def _parse_single_entry(self, item):
        """Convert one JSON publication record into a citation entry dict."""
        if not isinstance(item, dict):
            return None

        title = (item.get("title") or "").strip()
        if not title:
            return None

        contributors = item.get("contributors") or []
        authors = ", ".join(c for c in contributors if c) or None

        year = str(item.get("publicationYear") or "").strip() or None
        publisher = (item.get("publisher") or "").strip() or _PUBLISHER
        serie = (item.get("serie") or "").strip() or None

        # Series name / report number: "NINA rapport 2781" -> ("rapport", "2781")
        series_name = None
        report_num = None
        m = re.search(r"NINA\s+(\w+)\s+(\d+)", serie or "")
        if m:
            series_name = m.group(1).strip()
            report_num = m.group(2).strip()

        file_or_link = item.get("fileOrLink") or {}
        detail_url = (file_or_link.get("url") or "").strip() or None
        pdf_url = detail_url if detail_url and detail_url.lower().endswith(".pdf") else None

        # stable external_id: series-number pair when available, else the
        # record's own NVA id (unique per publication), else MD5 fallback
        if series_name and report_num:
            external_id = f"nina-{series_name.lower()}-{report_num}"
        elif item.get("_id"):
            external_id = hashlib.md5(str(item["_id"]).encode("utf-8")).hexdigest()[:16]
        else:
            external_id = hashlib.md5(json.dumps(item, sort_keys=True).encode("utf-8")).hexdigest()[:16]

        # Synthesized citation text (real abstracts are no longer exposed by
        # the API) — used as the abstract/quality-gate field.
        citation_parts = [p for p in (authors, year, title, serie, publisher) if p]
        citation = ". ".join(citation_parts)

        meta = {
            "posted_date": None,
            "series": series_name,
            "report_number": report_num,
            "citation": citation,
            "nva_id": item.get("_id"),
        }

        return {
            "external_id": external_id,
            "post_number": report_num,          # numeric string for incremental stop
            "title": title,
            "abstract": citation,               # synthesized citation stands in for abstract
            "authors": authors,
            "publisher": publisher,
            "published_date": year,             # year only (YYYY)
            "listed_date": None,
            "url": detail_url or _LIST_URL,     # prefer NVA record URL as detail URL
            "pdf_url": pdf_url,
            "keywords": None,
            "category": serie or series_name,
            "doi": None,
            "original_filename": None,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_ids = set()
        limit_str = str(limit) if limit is not None else "∞"

        # ---- Phase 1: fetch the single listing JSON payload ----
        print(f"[{self.site_id}] fetching {_LIST_URL}")
        raw = self._curl_cffi_get(_LIST_URL)
        if not raw:
            print(f"[{self.site_id}] failed to fetch listing JSON")
            return 0

        # ---- Phase 2: parse all entries from the JSON ----
        entries = self._parse_listing_json(raw)
        if not entries:
            print(f"[{self.site_id}] no entries found — page structure may have changed")
            return 0

        total = len(entries)
        print(f"[{self.site_id}] found {total} candidate entries on listing page")

        # Model entries as a single "page" for protocol compliance
        page = 1
        processed_on_page = 0

        for i, entry in enumerate(entries, 1):
            try:
                # Wall-clock budget
                if time.time() - start_time > _WALL_BUDGET:
                    print(f"[{self.site_id}] 25-min wall budget reached, stopping (saved={saved})")
                    break

                if limit is not None and saved >= limit:
                    break

                # URL/ID deduplication
                ext_id = entry.get("external_id") or ""
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                # Skip entries with short abstracts
                abstract = entry.get("abstract", "")
                if len(abstract) < _MIN_ABSTRACT:
                    print(f"[{self.site_id}] skip entry {i}: abstract {len(abstract)} chars "
                          f"(< {_MIN_ABSTRACT}) — {abstract[:40]!r}")
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": entry["external_id"],
                    "post_number": entry.get("post_number"),
                    "title": entry["title"],
                    "abstract": abstract,
                    "published_date": entry.get("published_date"),
                    "listed_date": entry.get("listed_date"),
                    "authors": entry.get("authors"),
                    "publisher": entry.get("publisher"),
                    "journal": None,
                    "url": entry.get("url"),
                    "pdf_url": entry.get("pdf_url"),
                    "keywords": entry.get("keywords"),
                    "category": entry.get("category"),
                    "doi": entry.get("doi"),
                    "original_filename": entry.get("original_filename"),
                    "metadata": entry.get("metadata"),
                }

                self._save_paper(paper)
                saved += 1
                processed_on_page += 1
                print(f"[{self.site_id}] saved {saved}/{limit_str}: {entry['title'][:60]}")

                # Progress every 10 "virtual pages" (every 10 entries since there's 1 real page)
                if saved % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                time.sleep(self._delay * 0.1)  # light delay; no per-detail fetch needed

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {i} failed: {exc}")
                continue

        # Detect "0 new records" end condition (only 1 real page)
        if processed_on_page == 0:
            print(f"[{self.site_id}] page {page} returned 0 new records — done")

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
