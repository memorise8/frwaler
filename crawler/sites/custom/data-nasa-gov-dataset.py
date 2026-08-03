# -*- coding: utf-8 -*-
"""Crawler for data.nasa.gov dataset catalogue (CKAN API).

Uses the public CKAN JSON API:
  https://data.nasa.gov/api/3/action/package_search?rows=100&start=0
Total catalogue: ~35 000 records; no auth required.
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _parse_iso_date(s):
    """Return YYYY-MM-DD from an ISO datetime string, or None."""
    if not s:
        return None
    s = s.strip()
    try:
        date_part = s.split("T")[0] if "T" in s else s[:10]
        datetime.strptime(date_part, "%Y-%m-%d")
        return date_part
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class DataNasaGovDatasetCrawler(BaseCrawler):
    """Crawl the NASA data.gov dataset catalogue via CKAN API."""

    # Class attributes (not @property) — required by spec.
    site_id = "data-nasa-gov-dataset"
    site_name = "Custom: data-nasa-gov-dataset"
    base_url = "https://data.nasa.gov"

    _API_BASE = "https://data.nasa.gov/api/3/action/package_search"
    _PAGE_SIZE = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _ABSTRACT_MIN_LEN = 100      # skip if abstract shorter than this
    _MAX_WALL_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))     # 25-minute budget

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_json(self, url, retries=3):
        """Fetch *url* via curl (TLS-flexible) and parse as JSON.

        Returns parsed dict/list on success, None on failure.
        Retries with exponential backoff: 1 s → 3 s → 9 s.
        """
        for attempt in range(retries):
            if attempt > 0:
                wait = 3 ** attempt
                print(f"[data-nasa-gov-dataset] retry {attempt}/{retries-1} in {wait}s ...")
                time.sleep(wait)
            try:
                proc = subprocess.run(
                    ["curl", "--tls-max", "1.3", "-sk", "--max-time", "30", url],
                    capture_output=True,
                    timeout=35,
                )
                if proc.returncode != 0 or not proc.stdout:
                    continue
                try:
                    text = proc.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    text = proc.stdout.decode("utf-8", errors="replace")
                return json.loads(text)
            except subprocess.TimeoutExpired:
                print(f"[data-nasa-gov-dataset] curl timeout (attempt {attempt+1}): {url[:80]}")
            except json.JSONDecodeError as exc:
                print(f"[data-nasa-gov-dataset] JSON parse error (attempt {attempt+1}): {exc}")
            except Exception as exc:
                print(f"[data-nasa-gov-dataset] fetch error (attempt {attempt+1}): {exc}")
        return None

    def _fetch_page(self, start):
        """Fetch one page of CKAN results starting at *start*.

        Returns list of package dicts, or None on failure, or [] at end.
        """
        url = (
            f"{self._API_BASE}"
            f"?rows={self._PAGE_SIZE}&start={start}&sort=metadata_created+desc"
        )
        data = self._curl_json(url)
        if data is None:
            return None
        if not data.get("success"):
            print(f"[data-nasa-gov-dataset] API returned success=false at start={start}")
            return None
        return data.get("result", {}).get("results", [])

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_package(self, pkg):
        """Map a CKAN package dict → paper_dict for _save_paper()."""
        extras = {e["key"]: e["value"] for e in pkg.get("extras", [])}

        pkg_id = pkg.get("id", "")
        name = pkg.get("name", "")
        title = (pkg.get("title") or "").strip()
        abstract = (pkg.get("notes") or "").strip()

        # Author / maintainer
        author = (pkg.get("author") or pkg.get("maintainer") or "").strip() or None

        # Publisher: extras["publisher"] > organization title
        org = pkg.get("organization") or {}
        publisher_raw = (
            extras.get("publisher")
            or org.get("title")
            or None
        )

        # Identifier & DOI (extras["identifier"] looks like "10.xxx/...")
        identifier = extras.get("identifier", "")
        doi = identifier if identifier.startswith("10.") else None

        # Dates
        published_date = _parse_iso_date(
            extras.get("modified") or pkg.get("metadata_modified")
        )
        posted_date = _parse_iso_date(pkg.get("metadata_created"))

        # Canonical detail URL
        slug = name or pkg_id
        detail_url = f"{self.base_url}/dataset/{slug}"

        # PDF attachment (most resources are ISO metadata; grab any PDF)
        pdf_url = None
        original_filename = None
        for res in pkg.get("resources", []):
            fmt = (res.get("format") or "").upper()
            res_url = res.get("url", "")
            if fmt == "PDF" or res_url.lower().endswith(".pdf"):
                pdf_url = res_url
                fn = res_url.rstrip("/").split("/")[-1].split("?")[0]
                if "." in fn:
                    original_filename = fn
                break

        # Keywords from CKAN tags
        tag_names = [t["name"] for t in pkg.get("tags", []) if t.get("name")]
        keywords = ", ".join(tag_names) if tag_names else None

        # Category: CKAN groups > type > resource-type extra
        category = None
        if pkg.get("groups"):
            labels = [
                (g.get("title") or g.get("name") or "")
                for g in pkg["groups"]
            ]
            labels = [lb for lb in labels if lb]
            if labels:
                category = "; ".join(labels)
        if not category:
            category = pkg.get("type") or extras.get("resource-type") or None

        # post_number: identifier slug is the most stable native ID
        post_number = identifier or name or pkg_id

        # Metadata JSON: remaining site-specific raw fields
        meta = {
            "ckan_id": pkg_id,
            "ckan_name": name,
            "identifier": identifier or None,
            "access_level": extras.get("accessLevel"),
            "resource_type": extras.get("resource-type"),
            "theme": extras.get("theme"),
            "harvest_source_title": extras.get("harvest_source_title"),
            "num_resources": pkg.get("num_resources"),
            "organization": org.get("name") or None,
            "license": extras.get("license"),
            "posted_date": posted_date,
            "originalFilename": original_filename,
        }
        meta = {k: v for k, v in meta.items() if v is not None}

        return {
            "external_id": pkg_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": posted_date,
            "authors": author,
            "publisher": publisher_raw,
            "url": detail_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl the NASA data.gov CKAN catalogue.

        Paginates through package_search, skips items with short abstracts,
        and saves up to *limit* records (None = unlimited).
        """
        saved = 0
        seen_urls = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page_num in range(self._MAX_PAGES):
            # Wall-clock budget check
            elapsed = time.time() - start_time
            if elapsed > self._MAX_WALL_SECS:
                print(
                    f"[data-nasa-gov-dataset] 25-minute wall-clock budget reached "
                    f"at page {page_num}, exiting cleanly."
                )
                break

            if limit is not None and saved >= limit:
                break

            # Safety-cap warning
            if page_num == self._MAX_PAGES - 1:
                print(
                    f"[data-nasa-gov-dataset] safety cap of {self._MAX_PAGES} pages reached."
                )

            # Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                print(
                    f"[data-nasa-gov-dataset] page {page_num}: "
                    f"saved {saved}/{limit_display}"
                )

            packages = self._fetch_page(page_num * self._PAGE_SIZE)
            if packages is None:
                print(f"[data-nasa-gov-dataset] page {page_num}: fetch failed, stopping.")
                break
            if not packages:
                print(f"[data-nasa-gov-dataset] page {page_num}: no more records.")
                break

            new_in_page = 0
            for pkg in packages:
                if limit is not None and saved >= limit:
                    break
                if time.time() - start_time > self._MAX_WALL_SECS:
                    break

                # URL deduplication
                slug = pkg.get("name") or pkg.get("id", "")
                detail_url = f"{self.base_url}/dataset/{slug}"
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_in_page += 1

                try:
                    paper = self._parse_package(pkg)

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < self._ABSTRACT_MIN_LEN:
                        print(
                            f"[data-nasa-gov-dataset] skip short abstract "
                            f"({len(abstract)} chars): {paper.get('title', '')[:60]!r}"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    snippet = (pkg.get("title") or "")[:60]
                    print(f"[data-nasa-gov-dataset] item failed ({snippet!r}): {exc}")
                    continue

            if new_in_page == 0:
                print(
                    f"[data-nasa-gov-dataset] page {page_num}: "
                    "all items already seen, stopping."
                )
                break

            # Light rate-limit between page fetches
            time.sleep(0.3)

        print(f"[data-nasa-gov-dataset] crawl complete: saved={saved}/{limit_display}")
        return saved
