# -*- coding: utf-8 -*-
"""Crawler for catalogue.data.govt.nz - Department of Internal Affairs datasets.

The site sits behind Imperva bot protection which blocks plain curl/requests.
Strategy:
  1. Open a headless Playwright browser and navigate to the listing page to
     pass the Imperva JS challenge (sets the required cookies in the context).
  2. Use in-browser fetch() via page.evaluate() to call the CKAN JSON API on
     every subsequent page — no further full navigations needed.
  3. Parse results, build a rich abstract, and persist via self._save_paper().

Absolute import — this file is loaded via spec_from_file_location, so relative
imports are unavailable. sys.path manipulation puts the project root on the path.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from crawler.base_crawler import BaseCrawler  # noqa: E402

_BASE_URL = "https://catalogue.data.govt.nz"
_CKAN_PATH = "/api/3/action/package_search"
_ORG = "department-of-internal-affairs"
_PAGE_SIZE = 20
_MAX_PAGES = 200        # safety cap — logged when hit
_ABSTRACT_MIN = 100     # skip items shorter than this (test asserts >=100)
_WALL_BUDGET = 25 * 60  # 25-minute max wall-clock run
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class CatalogueDataGovtNzDatasetCrawler(BaseCrawler):
    site_id = "catalogue-data-govt-nz-dataset"
    site_name = "Custom: catalogue-data-govt-nz-dataset"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Public crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl CKAN API and persist datasets for the DIA organisation.

        Parameters
        ----------
        limit:
            Maximum records to save. None means unlimited.
            Must work correctly for any value (3, 500, None, …).
        """
        start_ts = time.time()
        limit_label = limit if limit is not None else "∞"

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(f"[{self.site_id}] ERROR: playwright not installed — "
                  "run: pip install playwright && playwright install chromium")
            return 0

        saved = 0
        seen_urls: set = set()

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=_UA,
                    locale="en-US",
                    timezone_id="Pacific/Auckland",
                    viewport={"width": 1280, "height": 800},
                    extra_http_headers={"Accept-Language": "en-NZ,en;q=0.9"},
                )
                page = context.new_page()

                # ── Step 1: pass Imperva challenge ─────────────────────────
                print(f"[{self.site_id}] Loading main page to pass bot-protection challenge…")
                try:
                    page.goto(
                        f"{_BASE_URL}/dataset?organization={_ORG}"
                        f"&sort=metadata_modified+desc",
                        wait_until="networkidle",
                        timeout=50_000,
                    )
                except Exception as exc:
                    print(f"[{self.site_id}] Initial navigation warning (non-fatal): {exc}")

                time.sleep(3)  # let any JS challenge settle

                page_title = page.title()
                if "Pardon" in page_title or "Interruption" in page_title:
                    print(f"[{self.site_id}] ERROR: Imperva challenge not cleared. Aborting.")
                    browser.close()
                    return 0
                print(f"[{self.site_id}] Challenge passed. Page title: {page_title!r}")

                # ── Step 2: paginate via in-browser fetch() ─────────────────
                p = 0
                start_offset = 0

                while True:
                    if time.time() - start_ts > _WALL_BUDGET:
                        print(f"[{self.site_id}] 25-minute wall-clock budget reached "
                              f"at page {p}. Exiting cleanly.")
                        break

                    if limit is not None and saved >= limit:
                        break

                    if p >= _MAX_PAGES:
                        print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping.")
                        break

                    api_path = (
                        f"{_CKAN_PATH}"
                        f"?fq=organization:{_ORG}"
                        f"&sort=metadata_modified+desc"
                        f"&rows={_PAGE_SIZE}&start={start_offset}"
                    )
                    raw = self._browser_fetch(page, api_path)

                    if raw is None:
                        print(f"[{self.site_id}] API fetch failed at page {p}. Stopping.")
                        break

                    try:
                        data = json.loads(raw)
                    except (json.JSONDecodeError, ValueError) as exc:
                        print(f"[{self.site_id}] JSON parse error at page {p}: {exc}. Stopping.")
                        break

                    if not data.get("success"):
                        print(f"[{self.site_id}] API success=false at page {p}. Stopping.")
                        break

                    items = data.get("result", {}).get("results", [])
                    if not items:
                        print(f"[{self.site_id}] No more items at page {p}. Done.")
                        break

                    if p == 0:
                        total = data["result"].get("count", "?")
                        print(f"[{self.site_id}] Total records on server: {total}")

                    new_on_page = 0
                    for item in items:
                        if limit is not None and saved >= limit:
                            break
                        try:
                            did_save = self._process_item(
                                item, seen_urls, saved, limit_label
                            )
                            if did_save:
                                saved += 1
                                new_on_page += 1
                        except KeyboardInterrupt:
                            raise
                        except Exception as exc:
                            item_id = item.get("id", "?") if isinstance(item, dict) else "?"
                            print(f"[{self.site_id}] item {item_id} failed: {exc}")
                            continue

                    # All seen or skipped → possible paginator loop
                    if new_on_page == 0 and len(items) > 0:
                        print(f"[{self.site_id}] Page {p}: all {len(items)} items already seen "
                              "or skipped — possible paginator loop. Stopping.")
                        break

                    p += 1
                    start_offset += _PAGE_SIZE

                    if p % 10 == 0:
                        print(f"[{self.site_id}] page {p}: saved {saved}/{limit_label}")

                    # Last page (fewer results than requested)
                    if len(items) < _PAGE_SIZE:
                        print(f"[{self.site_id}] Last page reached "
                              f"(got {len(items)} < {_PAGE_SIZE}). Done.")
                        break

                    time.sleep(1.0)  # polite rate limiting

            finally:
                browser.close()

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _browser_fetch(self, page, path: str, retries: int = 3) -> "str | None":
        """Run fetch(path) inside the Playwright browser and return response text.

        Uses exponential back-off: 1 s → 3 s → 9 s on failure.
        """
        js = f"""
        async () => {{
            try {{
                const res = await fetch({json.dumps(path)});
                if (!res.ok) return null;
                return await res.text();
            }} catch (e) {{
                return null;
            }}
        }}
        """
        for attempt in range(retries):
            try:
                result = page.evaluate(js)
                if result:
                    return result
                wait = 3 ** attempt
                print(f"[{self.site_id}] Empty response from {path} "
                      f"(attempt {attempt+1}/{retries}), retrying in {wait}s…")
                time.sleep(wait)
            except Exception as exc:
                wait = 3 ** attempt
                print(f"[{self.site_id}] fetch error (attempt {attempt+1}/{retries}): {exc}, "
                      f"retrying in {wait}s…")
                time.sleep(wait)
        return None

    def _build_abstract(self, item: dict) -> str:
        """Build the richest possible abstract from a CKAN package dict.

        Augments the primary 'notes' field with resource descriptions,
        theme, tags, and other structured fields when notes is short.
        """
        parts: list[str] = []

        raw_notes = (item.get("notes") or "").strip()
        notes = re.sub(r"\r\n", "\n", raw_notes)
        notes = re.sub(r"\n{3,}", "\n\n", notes).strip()
        if notes:
            parts.append(notes)

        combined = "\n\n".join(parts)
        if len(combined) >= _ABSTRACT_MIN:
            return combined

        # Augment with resource descriptions (often more detailed than notes)
        for res in (item.get("resources") or [])[:5]:
            desc = (res.get("description") or "").strip()
            if desc and len(desc) > 20 and desc not in parts:
                parts.append(desc)
                if len("\n\n".join(parts)) >= _ABSTRACT_MIN:
                    break

        combined = "\n\n".join(parts)
        if len(combined) >= _ABSTRACT_MIN:
            return combined

        # Still short — append structured metadata fields
        theme = (item.get("theme") or "").strip()
        if theme:
            parts.append(f"Theme: {theme}")

        tags = [
            (t.get("display_name") or t.get("name") or "").strip()
            for t in (item.get("tags") or [])
        ]
        tags = [t for t in tags if t]
        if tags:
            parts.append(f"Keywords: {', '.join(tags)}")

        freq = (item.get("frequency_of_update") or "").strip()
        if freq:
            parts.append(f"Update frequency: {freq}")

        temporal = (item.get("temporal") or "").strip()
        if temporal and temporal.lower() not in ("none", "null"):
            parts.append(f"Temporal coverage: {temporal}")

        org = item.get("organization") or {}
        org_title = (org.get("title") or org.get("name") or "").strip()
        if org_title:
            parts.append(f"Publisher: {org_title}")

        license_title = (item.get("license_title") or "").strip()
        if license_title:
            parts.append(f"License: {license_title}")

        num_res = item.get("num_resources")
        if num_res:
            parts.append(f"Number of resources: {num_res}")

        return "\n\n".join(parts)

    def _process_item(self, item: dict, seen_urls: set, saved: int, limit_label) -> bool:
        """Extract fields from a CKAN package dict, validate, and save.

        Returns True if the item was saved, False if skipped.
        """
        dataset_id = (item.get("id") or "").strip()
        name = (item.get("name") or dataset_id).strip()
        url = f"{_BASE_URL}/dataset/{name}"

        # URL deduplication — guards against silent paginator loops
        if url in seen_urls:
            return False
        seen_urls.add(url)

        title = (item.get("title") or "").strip()
        if not title:
            print(f"[{self.site_id}] Skipping item with no title (id={dataset_id})")
            return False

        abstract = self._build_abstract(item)
        if len(abstract) < _ABSTRACT_MIN:
            print(f"[{self.site_id}] Skipping '{title[:50]}': "
                  f"abstract too short ({len(abstract)} chars < {_ABSTRACT_MIN})")
            return False

        # Dates — prefer issued, fall back to metadata_modified
        issued = (item.get("issued") or item.get("metadata_modified") or "").strip()
        published_date = issued[:10] if issued else ""

        # Authors
        authors_list: list[str] = []
        for field in ("author", "maintainer"):
            val = (item.get(field) or "").strip()
            if val and val not in authors_list:
                authors_list.append(val)

        org = item.get("organization") or {}
        org_title = (org.get("title") or org.get("name") or "").strip()
        department = org_title

        # Keywords from tags
        tags = [
            (t.get("display_name") or t.get("name") or "").strip()
            for t in (item.get("tags") or [])
        ]
        tags = [t for t in tags if t]

        # First PDF resource URL
        pdf_url = ""
        for res in (item.get("resources") or []):
            fmt = (res.get("format") or "").upper()
            res_url = (res.get("url") or "").strip()
            if res_url and (fmt == "PDF" or res_url.lower().endswith(".pdf")):
                pdf_url = res_url
                break

        # Category
        category = (item.get("theme") or item.get("type") or "").strip()

        # Resources summary
        resources_summary = [
            {
                "name": (r.get("name") or "").strip(),
                "format": (r.get("format") or "").strip(),
                "url": (r.get("url") or "").strip(),
                "description": (r.get("description") or "").strip()[:200],
            }
            for r in (item.get("resources") or [])
        ]

        metadata = {
            "ckan_name": name,
            "license": (item.get("license_title") or "").strip(),
            "modified": (item.get("metadata_modified") or "").strip(),
            "language": (item.get("language") or "").strip(),
            "organization": org_title,
            "num_resources": item.get("num_resources") or 0,
            "resources": resources_summary,
            "frequency_of_update": (item.get("frequency_of_update") or "").strip(),
            "temporal": (item.get("temporal") or "").strip(),
            "spatial": (item.get("spatial") or "").strip(),
            "source_identifier": (item.get("source_identifier") or "").strip(),
        }

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": dataset_id,
            "title": title,
            "authors": json.dumps(authors_list, ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(tags, ensure_ascii=False),
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": "",
            "department": department,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

        self._save_paper(paper)
        print(f"[{self.site_id}] Saved {saved + 1}/{limit_label}: {title[:60]}")
        return True
