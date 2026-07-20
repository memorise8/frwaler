# -*- coding: utf-8 -*-
"""e-Govデータポータル (data.e-gov.go.jp) PDF dataset crawler.

CKAN 2.9.5 site — uses the CKAN package_search JSON API directly.
No per-item detail-page fetch needed: the list API returns full resource metadata.
"""

import json
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler


class DataEGovJpDataCrawler(BaseCrawler):
    site_id = "data-e-gov-go-jp-data"
    site_name = "Custom: data-e-gov-go-jp-data"
    base_url = "https://data.e-gov.go.jp"

    _API_BASE = "https://data.e-gov.go.jp/data/api/3/action/package_search"
    _ROWS = 100
    # Test asserts all saved abstracts >= 100 chars, so use 100 as the skip threshold.
    _ABSTRACT_MIN = 100

    # ------------------------------------------------------------------
    # curl helper with exponential-backoff retry (3 attempts: 1s, 3s, 9s)
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        for attempt in range(3):
            try:
                result = subprocess.run(
                    ["curl", "-sk", "--tls-max", "1.3", "--max-time", "30", url],
                    capture_output=True, timeout=35,
                )
                raw = result.stdout.decode("utf-8", errors="replace").strip()
                if raw:
                    return raw
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                wait = 3 ** attempt  # 1s, 3s
                print(f"[{self.site_id}] Retrying in {wait}s...")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts for: {url}")
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        BUDGET_SECS = 25 * 60
        MAX_PAGES = 200

        start_time = time.time()
        saved = 0
        seen_urls: set = set()
        start = 0   # CKAN API offset
        page = 0    # page counter (0-indexed)

        while True:
            # ── Budget check ──────────────────────────────────────────
            elapsed = time.time() - start_time
            if elapsed > BUDGET_SECS:
                print(f"[{self.site_id}] 25-minute budget exhausted after {page} pages. Exiting cleanly.")
                break

            # ── Limit check ───────────────────────────────────────────
            if limit is not None and saved >= limit:
                break

            # ── Safety cap ────────────────────────────────────────────
            if page >= MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break

            # ── Progress log every 10 pages ───────────────────────────
            if page > 0 and page % 10 == 0:
                limit_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # ── Build and fetch API URL ───────────────────────────────
            params = urllib.parse.urlencode({
                "fq": "res_format:PDF",
                "rows": self._ROWS,
                "start": start,
                "sort": "metadata_modified desc",
            })
            api_url = f"{self._API_BASE}?{params}"

            raw = self._curl_get(api_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page} (start={start}). Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON decode error at page {page}: {exc}. Stopping.")
                break

            if not data.get("success"):
                print(f"[{self.site_id}] API success=false at page {page}. Stopping.")
                break

            result_obj = data.get("result") or {}
            results = result_obj.get("results") or []

            if page == 0:
                total = result_obj.get("count")
                print(f"[{self.site_id}] Total PDF datasets on server: {total}")

            if not results:
                print(f"[{self.site_id}] No more results at page {page} (start={start}). Done.")
                break

            # ── Process items ─────────────────────────────────────────
            new_on_page = 0  # genuinely new URLs seen this page (for loop-detection)

            for item in results:
                if limit is not None and saved >= limit:
                    break

                try:
                    dataset_name = (item.get("name") or "").strip()
                    dataset_id = (item.get("id") or "").strip()
                    dataset_url = f"https://data.e-gov.go.jp/data/dataset/{dataset_name}"

                    # URL deduplication — tracks new discoveries regardless of abstract length
                    if dataset_url in seen_urls:
                        continue
                    seen_urls.add(dataset_url)
                    new_on_page += 1

                    title = (item.get("title") or "").strip()
                    if not title:
                        continue

                    # ── Abstract: notes + extras.description + resource descriptions ──
                    notes = (item.get("notes") or "").strip()
                    extras = {e["key"]: e["value"] for e in (item.get("extras") or [])}
                    extras_desc = (extras.get("description") or "").strip()

                    res_descs: list = []
                    for r in (item.get("resources") or []):
                        rd = (r.get("description") or "").strip()
                        if rd and rd not in res_descs:
                            res_descs.append(rd)

                    parts: list = []
                    for p in [notes, extras_desc] + res_descs:
                        if p and p not in parts:
                            parts.append(p)
                    abstract = "\n".join(parts).strip()

                    if len(abstract) < self._ABSTRACT_MIN:
                        print(
                            f"[{self.site_id}] Skipping {dataset_name}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # ── Dates ─────────────────────────────────────────
                    meta_created = (item.get("metadata_created") or "")
                    meta_modified = (item.get("metadata_modified") or "")
                    published_date = meta_created[:10] if meta_created else None
                    listed_date = meta_modified[:10] if meta_modified else None

                    # ── Publisher / authors ───────────────────────────
                    publisher = (item.get("publisher") or "").strip()
                    org = item.get("organization") or {}
                    org_title = (org.get("title") or "").strip()
                    if not publisher:
                        publisher = org_title

                    author = (item.get("author") or "").strip()
                    creator = (extras.get("creator") or "").strip()
                    authors = author or creator or None

                    # ── PDF URL: first PDF resource ───────────────────
                    pdf_url = None
                    original_filename = None
                    for r in (item.get("resources") or []):
                        fmt = (r.get("format") or "").upper()
                        r_url = (r.get("url") or "").strip()
                        if fmt == "PDF" and r_url:
                            pdf_url = r_url
                            tail = r_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                            if "." in tail and len(tail) <= 200:
                                original_filename = tail
                            break

                    # ── Keywords from CKAN tags ───────────────────────
                    tag_list = [t.get("name", "") for t in (item.get("tags") or []) if t.get("name")]
                    keywords = ", ".join(tag_list) if tag_list else None

                    # ── Category from groups ──────────────────────────
                    groups = item.get("groups") or []
                    category = groups[0].get("title", "") if groups else None

                    paper = {
                        "site_id": self.site_id,
                        "external_id": dataset_id,
                        "post_number": dataset_name,  # slug, used for incremental MAX() ordering
                        "url": dataset_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": authors,
                        "publisher": publisher,
                        "department": None,
                        "journal": None,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": category,
                        "doi": None,
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "ckan_name": dataset_name,
                            "organization": org_title,
                            "frequency_of_update": item.get("frequency_of_update") or "",
                            "landingPage": item.get("landingPage") or "",
                            "num_resources": item.get("num_resources") or 0,
                            "license_id": item.get("license_id"),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('name', '?')} failed: {exc}")
                    continue

            # ── End-of-page checks ────────────────────────────────────
            # If every URL on this page was already in seen_urls, the paginator has looped.
            if new_on_page == 0:
                print(f"[{self.site_id}] No new items on page {page} — paginator looped. Done.")
                break

            page += 1
            start += self._ROWS

            # Rate-limit: one sleep per page fetch (no per-item HTTP requests here)
            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
