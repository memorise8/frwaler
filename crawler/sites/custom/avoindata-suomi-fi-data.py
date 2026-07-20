# -*- coding: utf-8 -*-
"""Crawler for avoindata.suomi.fi – Finnish open data portal (PDF datasets).

Uses CKAN REST API: /data/api/3/action/package_search?fq=res_format:pdf
All fields come from the list API — no per-document detail fetch needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from urllib.parse import urlencode

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from crawler.base_crawler import BaseCrawler

_PAGE_SIZE = 100
_MAX_PAGES = 200
_CRAWL_TIMEOUT = 25 * 60  # seconds


class AvoindataSuomiFiDataCrawler(BaseCrawler):
    site_id = "avoindata-suomi-fi-data"
    site_name = "Custom: avoindata-suomi-fi-data"
    base_url = "https://avoindata.suomi.fi"

    _API_URL = "https://avoindata.suomi.fi/data/api/3/action/package_search"

    def _curl_json(self, url: str, retries: int = 3):
        """Fetch URL via curl and return parsed JSON, or None on failure."""
        for attempt in range(retries):
            try:
                proc = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", "-L", "--max-time", "30", url],
                    capture_output=True,
                    timeout=60,
                )
                if proc.returncode == 0 and proc.stdout:
                    text = proc.stdout.decode("utf-8", errors="replace")
                    return json.loads(text)
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/{retries}): {exc}")
            if attempt < retries - 1:
                wait = (1, 3, 9)[attempt]
                print(f"[{self.site_id}] retrying in {wait}s…")
                time.sleep(wait)
        return None

    def _api_page(self, start: int, rows: int):
        """Return (results_list, total_count) from CKAN package_search."""
        qs = urlencode({"fq": "res_format:pdf", "rows": rows, "start": start})
        data = self._curl_json(f"{self._API_URL}?{qs}")
        if not data or not data.get("success"):
            return [], 0
        r = data.get("result") or {}
        return r.get("results") or [], r.get("count") or 0

    def _build_record(self, pkg: dict) -> dict | None:
        """Extract a record from a CKAN package dict. Returns None to skip."""
        # Title — prefer Finnish translation
        tt = pkg.get("title_translated") or {}
        title = (tt.get("fi") or tt.get("en") or pkg.get("title") or "").strip()
        if not title:
            return None

        # Abstract — prefer Finnish translation
        nt = pkg.get("notes_translated") or {}
        abstract = (nt.get("fi") or nt.get("en") or pkg.get("notes") or "").strip()

        # IDs
        external_id = pkg.get("id") or ""
        post_number = pkg.get("name") or external_id

        # Dates
        valid_from = (pkg.get("valid_from") or "")
        meta_created = pkg.get("metadata_created") or ""
        meta_modified = pkg.get("metadata_modified") or ""
        published_date = (valid_from[:10] if len(valid_from) >= 10 else None) or (meta_created[:10] if meta_created else None)
        listed_date = meta_created[:10] if meta_created else None

        # Authorship
        author = (pkg.get("author") or "").strip() or None
        maintainer = (pkg.get("maintainer") or "").strip()
        org = pkg.get("organization") or {}
        org_title = (org.get("title") or org.get("name") or "").strip()
        publisher = maintainer or org_title or None

        # Keywords
        kw_dict = pkg.get("keywords") or {}
        kw_list = kw_dict.get("fi") or kw_dict.get("en") or []
        if not kw_list:
            kw_list = [
                t.get("display_name") or t.get("name", "")
                for t in (pkg.get("tags") or [])
                if isinstance(t, dict)
            ]
        keywords = ", ".join(str(k) for k in kw_list if k) or None

        # Category from groups
        cat_parts = []
        for g in (pkg.get("groups") or []):
            if not isinstance(g, dict):
                continue
            extras = g.get("__extras") or {}
            tt_g = extras.get("title_translated") or {}
            label = tt_g.get("en") or tt_g.get("fi") or g.get("display_name") or g.get("name")
            if label:
                cat_parts.append(label)
        category = "; ".join(cat_parts) or None

        # First PDF resource
        pdf_url = None
        original_filename = None
        resource_desc = ""
        for res in (pkg.get("resources") or []):
            if not isinstance(res, dict):
                continue
            if (res.get("format") or "").upper() != "PDF":
                continue
            archiver = res.get("archiver") or {}
            pdf_url = archiver.get("cache_url") or res.get("url") or ""
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if "." in tail and len(tail) <= 255:
                original_filename = tail
            rd = res.get("description_translated") or {}
            resource_desc = (rd.get("fi") or rd.get("en") or res.get("description") or "").strip()
            break

        # Augment short abstract with resource description
        if len(abstract) < 200 and resource_desc and resource_desc not in abstract:
            abstract = (abstract + "\n\n" + resource_desc).strip()

        if len(abstract) < 50:
            print(f"[{self.site_id}] skip short abstract ({len(abstract)} chars): {title[:60]}")
            return None

        detail_url = f"{self.base_url}/data/fi/dataset/{external_id}"

        metadata = {
            "posted_date": meta_created,
            "originalFilename": original_filename,
            "metadata_modified": meta_modified,
            "valid_from": valid_from,
            "license_id": pkg.get("license_id"),
            "license_title": pkg.get("license_title"),
            "collection_type": pkg.get("collection_type"),
            "geographical_coverage": pkg.get("geographical_coverage"),
            "organization_name": org.get("name"),
            "organization_type": org.get("producer_type"),
            "maintainer_website": pkg.get("maintainer_website"),
            "maintainer_email": pkg.get("maintainer_email"),
            "ckan_name": post_number,
        }

        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "url": detail_url,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": author,
            "publisher": publisher,
            "keywords": keywords,
            "category": category,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def crawl(self, limit=None) -> int:
        """Crawl PDF datasets from avoindata.suomi.fi via CKAN API."""
        saved = 0
        seen_ids: set = set()
        limit_n = float("inf") if limit is None else limit
        start_time = time.time()
        page = 0

        try:
            while saved < limit_n:
                if time.time() - start_time > _CRAWL_TIMEOUT:
                    print(f"[{self.site_id}] 25-minute budget reached — stopping.")
                    break

                if page >= _MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached.")
                    break

                start_offset = page * _PAGE_SIZE
                results, total = self._api_page(start=start_offset, rows=_PAGE_SIZE)

                if not results:
                    print(f"[{self.site_id}] Empty page at offset {start_offset}, stopping.")
                    break

                new_on_page = 0
                for pkg in results:
                    if saved >= limit_n:
                        break
                    pkg_id = pkg.get("id") or pkg.get("name") or ""
                    if pkg_id in seen_ids:
                        continue
                    seen_ids.add(pkg_id)
                    new_on_page += 1

                    try:
                        record = self._build_record(pkg)
                        if record is None:
                            continue
                        self._save_paper(record)
                        saved += 1
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        label = (pkg.get("title") or pkg.get("id") or "?")[:50]
                        print(f"[{self.site_id}] item '{label}' failed: {exc}")
                        continue

                if page % 10 == 0:
                    limit_label = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_label}")

                if new_on_page == 0:
                    print(f"[{self.site_id}] No new records on page {page}, stopping.")
                    break

                if start_offset + len(results) >= total:
                    break

                page += 1
                time.sleep(0.5)

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. {saved} records saved.")
        return saved
