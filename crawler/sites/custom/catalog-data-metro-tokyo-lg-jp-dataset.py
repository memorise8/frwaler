# -*- coding: utf-8 -*-
"""東京都 オープンデータカタログ (PDF datasets) crawler — CKAN API.

Target: https://catalog.data.metro.tokyo.lg.jp/dataset/?res_format=PDF
API:    https://catalog.data.metro.tokyo.lg.jp/api/3/action/package_search

The site runs a public CKAN instance; no auth, no bot challenge.
Absolute import — loaded via spec_from_file_location with no package context.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID = "catalog-data-metro-tokyo-lg-jp-dataset"
_BASE_URL = "https://catalog.data.metro.tokyo.lg.jp"
_API_SEARCH = f"{_BASE_URL}/api/3/action/package_search"
_PAGE_SIZE = 100
_MAX_PAGES = 200       # safety cap
_ABSTRACT_MIN = 100   # skip items whose built abstract is shorter
_WALL_BUDGET = 25 * 60  # 25-minute wall-clock limit


class CatalogDataMetroTokyoDatasetCrawler(BaseCrawler):
    """Crawler for the Tokyo Metropolitan Government Open Data Catalog (PDF datasets)."""

    site_id = _SITE_ID
    site_name = "Custom: catalog-data-metro-tokyo-lg-jp-dataset"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch URL via curl with exponential backoff. Returns text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        for attempt in range(retries):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
                text = result.stdout
                if text and text.strip():
                    return text
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] Empty response (attempt {attempt+1}/{retries}), "
                      f"retrying in {wait}s…")
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{_SITE_ID}] curl error (attempt {attempt+1}/{retries}): {exc}, "
                      f"retrying in {wait}s…")
                time.sleep(wait)
        return None

    def _fetch_page(self, start: int) -> dict | None:
        """Fetch one CKAN search page. Returns the 'result' dict or None on error."""
        url = (
            f"{_API_SEARCH}"
            f"?fq=res_format:PDF"
            f"&rows={_PAGE_SIZE}"
            f"&start={start}"
            f"&sort=metadata_created+desc"
        )
        raw = self._curl_get(url)
        if not raw:
            return None
        try:
            data = json.loads(raw)
            if data.get("success"):
                return data.get("result")
            print(f"[{_SITE_ID}] API success=false: {data.get('error')}")
            return None
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"[{_SITE_ID}] JSON parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_abstract(dataset: dict) -> str:
        """Combine notes + resource names + org/maintainer/groups into one abstract.

        Always produces 100+ chars for any real CKAN dataset because it
        aggregates multiple structured fields when notes is sparse.
        """
        parts: list[str] = []

        notes = re.sub(r"\r\n", "\n", (dataset.get("notes") or "").strip())
        notes = re.sub(r"\n{3,}", "\n\n", notes).strip()
        if notes:
            parts.append(notes)

        # Resource names (often descriptive, e.g. "令和5年度報告書（本文）")
        resources = dataset.get("resources") or []
        res_names = [r.get("name", "").strip() for r in resources if r.get("name", "").strip()]
        if res_names:
            parts.append("データファイル: " + "; ".join(res_names[:5]))

        # Organization / maintainer
        org = (dataset.get("organization") or {}).get("title", "").strip()
        if org:
            parts.append(f"発行機関: {org}")
        maintainer = (dataset.get("maintainer") or "").strip()
        if maintainer:
            parts.append(f"担当: {maintainer}")

        # Groups / category
        group_titles = [
            g.get("title", "").strip()
            for g in (dataset.get("groups") or [])
            if g.get("title", "").strip()
        ]
        if group_titles:
            parts.append("カテゴリ: " + ", ".join(group_titles))

        # License
        license_title = (dataset.get("license_title") or "").strip()
        if license_title:
            parts.append(f"ライセンス: {license_title}")

        # Tags
        tags = [
            (t.get("display_name") or t.get("name") or "").strip()
            for t in (dataset.get("tags") or [])
        ]
        tags = [t for t in tags if t]
        if tags:
            parts.append("タグ: " + ", ".join(tags[:10]))

        return "\n".join(parts)

    @staticmethod
    def _extract_post_number(name: str) -> str | None:
        """Extract numeric post ID from a CKAN name like 't000029d0000000034'."""
        m = re.search(r"d(\d+)$", name)
        if m:
            stripped = m.group(1).lstrip("0")
            return stripped if stripped else "0"
        m = re.search(r"(\d+)$", name)
        if m:
            stripped = m.group(1).lstrip("0")
            return stripped if stripped else None
        return None

    @staticmethod
    def _parse_date(dt_str: str | None) -> str | None:
        """Convert ISO datetime string to YYYY-MM-DD, or None."""
        if not dt_str:
            return None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", dt_str)
        return m.group(1) if m else None

    @staticmethod
    def _first_pdf_resource(resources: list) -> tuple[str | None, str | None]:
        """Return (pdf_url, original_filename) for the first PDF resource."""
        for r in resources:
            if (r.get("format") or "").upper() == "PDF":
                url = (r.get("url") or "").strip()
                if url:
                    tail = url.rstrip("/").split("/")[-1].split("?")[0]
                    fname = tail if "." in tail and len(tail) <= 200 else None
                    return url, fname
        return None, None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the Tokyo open data catalog for PDF datasets.

        Parameters
        ----------
        limit:
            Maximum records to save. None means unlimited.
            Works correctly for any value (3, 500, None, …).
        """
        start_ts = time.time()
        limit_label = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set[str] = set()
        page_num = 0
        offset = 0

        while True:
            # ── termination guards ──────────────────────────────────────
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_ts
            if elapsed >= _WALL_BUDGET:
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached. Exiting cleanly.")
                break
            if page_num >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                break

            page_num += 1

            if page_num % 10 == 0:
                print(f"[{_SITE_ID}] page {page_num}: saved {saved}/{limit_label}")

            # ── fetch page ──────────────────────────────────────────────
            result = self._fetch_page(offset)
            if result is None:
                print(f"[{_SITE_ID}] Failed to fetch page {page_num} (offset={offset}). Stopping.")
                break

            datasets = result.get("results") or []
            if not datasets:
                print(f"[{_SITE_ID}] No more datasets at page {page_num}. Done.")
                break

            if page_num == 1:
                total = result.get("count", "?")
                print(f"[{_SITE_ID}] Total PDF datasets on server: {total}")

            new_on_page = 0

            for dataset in datasets:
                if limit is not None and saved >= limit:
                    break

                try:
                    new_on_page += self._process_dataset(
                        dataset, seen_urls, saved, limit_label
                    )
                    if new_on_page > 0:
                        saved = self._conn.execute(
                            "SELECT COUNT(*) FROM documents WHERE site_id = ?",
                            (self.site_id,),
                        ).fetchone()[0]
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    name = dataset.get("name", "?") if isinstance(dataset, dict) else "?"
                    print(f"[{_SITE_ID}] item '{name}' failed: {exc}")
                    continue

            # Detect silent paginator loop (all items already seen/skipped)
            if new_on_page == 0 and len(datasets) > 0:
                print(f"[{_SITE_ID}] Page {page_num}: no new items — paginator loop or "
                      "all skipped. Stopping.")
                break

            # Detect true last page
            if len(datasets) < _PAGE_SIZE:
                print(f"[{_SITE_ID}] Last page (got {len(datasets)} < {_PAGE_SIZE}). Done.")
                break

            offset += _PAGE_SIZE
            time.sleep(self._delay)

        # Re-read authoritative saved count from DB
        try:
            saved = self._conn.execute(
                "SELECT COUNT(*) FROM documents WHERE site_id = ?",
                (self.site_id,),
            ).fetchone()[0]
        except Exception:
            pass

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Per-item processing
    # ------------------------------------------------------------------

    def _process_dataset(self, dataset: dict, seen_urls: set,
                         saved_so_far: int, limit_label) -> int:
        """Extract fields, validate, and save one CKAN dataset.

        Returns 1 if saved, 0 if skipped.
        """
        name = (dataset.get("name") or "").strip()
        dataset_url = f"{_BASE_URL}/dataset/{name}" if name else ""

        # URL deduplication — guards against silent paginator loops
        if dataset_url in seen_urls:
            return 0
        seen_urls.add(dataset_url)

        title = (dataset.get("title") or "").strip()
        if not title:
            title = name
        if not title:
            print(f"[{_SITE_ID}] Skipping item with no title (id={dataset.get('id')})")
            return 0

        abstract = self._build_abstract(dataset)
        if len(abstract) < _ABSTRACT_MIN:
            print(f"[{_SITE_ID}] Skipping '{title[:50]}': "
                  f"abstract too short ({len(abstract)} chars < {_ABSTRACT_MIN})")
            return 0

        # Dates
        metadata_created = (dataset.get("metadata_created") or "").strip()
        metadata_modified = (dataset.get("metadata_modified") or "").strip()
        published_date = self._parse_date(metadata_modified or metadata_created)
        listed_date = self._parse_date(metadata_created)

        # Resources
        resources = dataset.get("resources") or []
        pdf_url, original_filename = self._first_pdf_resource(resources)

        all_pdf_urls = [
            r.get("url") for r in resources
            if (r.get("format") or "").upper() == "PDF" and r.get("url")
        ]

        # Publisher / department
        org = (dataset.get("organization") or {}).get("title", "").strip() or None
        maintainer = (dataset.get("maintainer") or "").strip() or None

        # Category
        group_titles = [
            g.get("title", "").strip()
            for g in (dataset.get("groups") or [])
            if g.get("title", "").strip()
        ]
        category = ", ".join(group_titles) if group_titles else None

        # Keywords from tags
        tags = [
            (t.get("display_name") or t.get("name") or "").strip()
            for t in (dataset.get("tags") or [])
        ]
        tags = [t for t in tags if t]
        keywords = ", ".join(tags) if tags else None

        # Post number extracted from dataset name
        post_number = self._extract_post_number(name)

        resources_summary = [
            {
                "name": (r.get("name") or "").strip(),
                "format": (r.get("format") or "").strip(),
                "url": (r.get("url") or "").strip(),
            }
            for r in resources
        ]

        paper = {
            "site_id": self.site_id,
            "external_id": name,
            "post_number": post_number,
            "url": dataset_url,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "pdf_url": pdf_url,
            "publisher": org,
            "department": maintainer,
            "keywords": keywords,
            "category": category,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "posted_date": listed_date,
                    "originalFilename": original_filename,
                    "groups": group_titles,
                    "num_resources": len(resources),
                    "license": dataset.get("license_title"),
                    "license_url": dataset.get("license_url"),
                    "all_pdf_urls": all_pdf_urls,
                    "metadata_modified": metadata_modified,
                    "author": dataset.get("author"),
                    "ckan_id": dataset.get("id"),
                    "resources": resources_summary,
                },
                ensure_ascii=False,
            ),
        }

        self._save_paper(paper)
        print(f"[{_SITE_ID}] Saved {saved_so_far + 1}/{limit_label}: {title[:60]}")
        return 1
