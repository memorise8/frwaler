# -*- coding: utf-8 -*-
"""opendata.dk PDF dataset crawler — CKAN API at ckan.oddk.prod.datopian.com.

The search portal (www.opendata.dk) is a Datopian/Express front-end that
proxies a CKAN backend.  The backend exposes a standard CKAN REST API that
we hit directly, giving clean JSON without any HTML parsing.

API entry-point:
    https://ckan.oddk.prod.datopian.com/api/3/action/package_search
        ?fq=res_format:PDF&rows=<N>&start=<offset>

Each *package* (dataset) is one record; a package may have several PDF
resources — we store the first PDF resource URL as pdf_url and let the
downloader handle the rest.
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler

_CKAN_API = "https://ckan.oddk.prod.datopian.com/api/3/action/package_search"
_PORTAL_BASE = "https://www.opendata.dk"
_ROWS_PER_PAGE = 20
_MAX_PAGES = 200
_ABSTRACT_MIN_CHARS = 100      # skip items with less; test asserts >= 100
_CRAWL_TIMEOUT_SECS = 25 * 60  # 25-minute wall-clock budget


class OpendataDkSearchCrawler(BaseCrawler):
    """Crawler for opendata.dk PDF datasets."""

    site_id = "opendata-dk-search"
    site_name = "Custom: opendata-dk-search"
    base_url = "https://www.opendata.dk"

    # ------------------------------------------------------------------ #
    # Network helpers
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str, max_time: int = 30) -> str | None:
        """GET via curl with up to 3 retries (1 s, 3 s, 9 s back-off)."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", str(max_time),
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json, */*;q=0.8",
            url,
        ]
        for attempt in range(3):
            try:
                res = subprocess.run(
                    cmd, capture_output=True, text=True,
                    timeout=max_time + 5, errors="replace",
                )
                if res.stdout.strip():
                    return res.stdout
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                wait = 3 ** attempt  # 1 s, 3 s
                time.sleep(wait)
        return None

    def _fetch_page(self, start: int) -> dict | None:
        """Fetch one page of CKAN results; return parsed dict or None."""
        url = (
            f"{_CKAN_API}"
            f"?fq=res_format:PDF"
            f"&rows={_ROWS_PER_PAGE}"
            f"&start={start}"
            f"&sort=metadata_modified+desc"
        )
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_date(iso_str: str) -> str:
        """Extract YYYY-MM-DD from an ISO datetime string."""
        if not iso_str:
            return ""
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(iso_str))
        return m.group(1) if m else ""

    @staticmethod
    def _extract_filename(url: str) -> str:
        """Return the last non-empty path segment of a URL."""
        if not url:
            return ""
        try:
            path = urlparse(url).path
            return os.path.basename(path.rstrip("/")) if path else ""
        except Exception:
            return ""

    def _build_abstract(self, pkg: dict) -> str:
        """Combine notes + notes_translated into the best available abstract."""
        parts: list[str] = []

        notes = (pkg.get("notes") or "").strip()
        if notes:
            parts.append(notes)

        notes_tr = pkg.get("notes_translated") or {}
        if isinstance(notes_tr, dict):
            for lang_text in notes_tr.values():
                if isinstance(lang_text, str):
                    t = lang_text.strip()
                    if t and t not in parts:
                        parts.append(t)

        return "\n\n".join(parts)

    def _pkg_to_paper(self, pkg: dict) -> dict | None:
        """Convert a CKAN package dict to a paper dict.

        Returns None (and logs) when the abstract is too short to be useful.
        """
        pkg_id = pkg.get("id", "")
        pkg_name = pkg.get("name", "")
        title = (pkg.get("title") or "").strip()

        abstract = self._build_abstract(pkg)
        if len(abstract) < _ABSTRACT_MIN_CHARS:
            print(
                f"[{self.site_id}] skipping '{title[:60]}': "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        # Organisation
        org = pkg.get("organization") or {}
        org_name = org.get("name", "")
        org_title = org.get("title", "")
        publisher = org_title or org_name or (pkg.get("author") or "")
        author = pkg.get("author") or ""

        # Dates
        published_date = self._parse_date(pkg.get("metadata_created", ""))
        listed_date = self._parse_date(pkg.get("metadata_modified", ""))

        # Canonical dataset page on the portal
        url = (
            f"{_PORTAL_BASE}/{org_name}/{pkg_name}"
            if org_name and pkg_name
            else ""
        )

        # First PDF resource
        pdf_url = ""
        original_filename = ""
        for res in pkg.get("resources") or []:
            if (res.get("format") or "").upper() == "PDF":
                pdf_url = res.get("url") or ""
                original_filename = (
                    res.get("name")
                    or self._extract_filename(pdf_url)
                )
                break
        if not original_filename and pdf_url:
            original_filename = self._extract_filename(pdf_url)

        # Tags → keywords (comma-separated)
        tags = [t.get("name", "") for t in (pkg.get("tags") or []) if t.get("name")]
        keywords = ",".join(tags)

        # Groups → category (comma-separated)
        groups = [g.get("name", "") for g in (pkg.get("groups") or []) if g.get("name")]
        category = ",".join(groups)

        # Resources summary for metadata
        resources_meta = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "format": r.get("format"),
                "url": r.get("url"),
                "created": r.get("created"),
                "last_modified": r.get("last_modified"),
            }
            for r in (pkg.get("resources") or [])
        ]

        metadata = {
            "posted_date": pkg.get("metadata_modified"),
            "originalFilename": original_filename,
            "pkg_id": pkg_id,
            "pkg_name": pkg_name,
            "org_name": org_name,
            "update_frequency": pkg.get("update_frequency"),
            "license_id": pkg.get("license_id"),
            "num_resources": pkg.get("num_resources"),
            "resources": resources_meta,
        }
        # Drop None-valued keys to keep the JSON lean
        metadata = {k: v for k, v in metadata.items() if v is not None}

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": pkg_id,
            "post_number": pkg_name,  # no numeric ID — use slug
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": author,
            "publisher": publisher,
            "url": url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "original_filename": original_filename,
            "doi": "",
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        """Crawl opendata.dk PDF datasets via CKAN package_search API.

        Paginates through results (20 per page) until saved >= limit,
        no new results, or safety caps are hit.
        """
        saved = 0
        page = 0
        seen_ids: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        while True:
            if limit is not None and saved >= limit:
                break

            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > _CRAWL_TIMEOUT_SECS:
                print(f"[{self.site_id}] Time budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # Fetch with retry
            data = None
            for retry in range(3):
                data = self._fetch_page(page * _ROWS_PER_PAGE)
                if data is not None:
                    break
                wait = (retry + 1) * 3
                print(
                    f"[{self.site_id}] page {page} fetch failed, "
                    f"retry {retry + 1}/3 in {wait}s..."
                )
                time.sleep(wait)

            if data is None:
                print(f"[{self.site_id}] page {page} failed after 3 retries. Stopping.")
                break

            if not data.get("success"):
                print(f"[{self.site_id}] CKAN API success=false at page {page}. Stopping.")
                break

            result = data.get("result") or {}
            packages = result.get("results") or []
            total_count = result.get("count", 0)

            if page == 0:
                print(f"[{self.site_id}] Total packages with PDF resources: {total_count}")

            if not packages:
                print(f"[{self.site_id}] No more packages at page {page}. Done.")
                break

            # Detect silent pagination loop (all IDs already seen)
            new_on_page = [p.get("id") for p in packages if p.get("id") not in seen_ids]
            if not new_on_page:
                print(f"[{self.site_id}] All items on page {page} already seen. Stopping.")
                break

            for pkg in packages:
                if limit is not None and saved >= limit:
                    break

                pkg_id = pkg.get("id", "")
                if not pkg_id or pkg_id in seen_ids:
                    continue
                seen_ids.add(pkg_id)

                try:
                    paper = self._pkg_to_paper(pkg)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {paper['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {pkg_id} failed: {exc}")
                    continue

            time.sleep(1.0)  # polite pause between pages
            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
