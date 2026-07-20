# -*- coding: utf-8 -*-
"""FAIR.healthdata.be dataset crawler (DKAN open-data portal).

API: GET /api/1/search?fulltext=&rows={n}&start={offset}
     Returns JSON {"total": "286", "results": {"dkan_dataset/{uuid}": {...}, ...}}
     All needed fields are in the search response; no per-item detail fetch required.
"""

import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler


class FairHealthdataBeSearchCrawler(BaseCrawler):
    """Crawler for https://fair.healthdata.be/search (DKAN FAIR data portal)."""

    site_id = "fair-healthdata-be-search"
    site_name = "Custom: fair-healthdata-be-search"
    base_url = "https://fair.healthdata.be"

    _SEARCH_API = "https://fair.healthdata.be/api/1/search"
    _PAGE_SIZE = 20
    _MAX_PAGES = 200

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with 3-attempt exponential-backoff retry."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
                if result.stdout.strip():
                    return result.stdout
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3): {exc}")
            if attempt < 2:
                wait = (1, 3, 9)[attempt]
                print(f"[{self.site_id}] Retrying in {wait}s…")
                time.sleep(wait)
        return None

    @staticmethod
    def _strip_html(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;", " ", text)
        text = re.sub(r"&amp;", "&", text)
        text = re.sub(r"&lt;", "<", text)
        text = re.sub(r"&gt;", ">", text)
        text = re.sub(r"&[a-zA-Z0-9#]+;", "", text)
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _extract_list_field(field) -> list:
        """Flatten keyword/theme fields — handles plain strings, lists, and ref-id dicts."""
        if not field:
            return []
        if isinstance(field, str):
            return [field.strip()] if field.strip() else []
        if isinstance(field, list):
            result = []
            for item in field:
                if isinstance(item, str):
                    if item.strip():
                        result.append(item.strip())
                elif isinstance(item, dict):
                    val = item.get("data") or item.get("name") or ""
                    if isinstance(val, str) and val.strip():
                        result.append(val.strip())
                    elif isinstance(val, dict):
                        name = val.get("name", "")
                        if name:
                            result.append(str(name).strip())
            return result
        return []

    @staticmethod
    def _extract_publisher(field) -> str:
        if not field:
            return ""
        if isinstance(field, str):
            return field.strip()
        if isinstance(field, dict):
            name = field.get("name", "")
            if name:
                return str(name).strip()
            data = field.get("data") or {}
            if isinstance(data, dict):
                return str(data.get("name", "")).strip()
        return ""

    def _build_abstract(self, item: dict, title: str, identifier: str) -> str:
        """Build a rich abstract that guarantees >= 100 chars for any valid dataset."""
        parts = []

        # 1. Title preamble (always present)
        parts.append(f"Dataset: {title}")

        # 2. Description (strip HTML)
        desc = self._strip_html(item.get("description") or "")
        if len(desc) >= 20:
            parts.append(desc)

        # 3. Distribution / resource summary
        distrib_titles = []
        for dist in (item.get("distribution") or []):
            if not isinstance(dist, dict):
                continue
            # Search results use the direct dcat:Distribution structure
            d_title = (dist.get("title") or dist.get("description") or "").strip()
            fmt = (dist.get("format") or dist.get("mediaType") or "").strip()
            if d_title:
                distrib_titles.append(f"{d_title} [{fmt}]" if fmt else d_title)
        if distrib_titles:
            parts.append("Resources: " + "; ".join(distrib_titles[:15]))

        # 4. Catalog metadata footer (always verbose to guarantee length)
        catalog = []
        if identifier:
            catalog.append(f"Identifier: {identifier}")
        modified = (item.get("modified") or "").strip()
        if modified:
            catalog.append(f"Modified: {modified}")
        access = (item.get("accessLevel") or "").strip()
        if access:
            catalog.append(f"Access: {access}")
        theme_list = self._extract_list_field(item.get("theme"))
        if theme_list:
            catalog.append("Theme: " + ", ".join(theme_list))
        kw_list = self._extract_list_field(item.get("keyword"))
        if kw_list:
            catalog.append("Keywords: " + ", ".join(kw_list[:10]))
        publisher = self._extract_publisher(item.get("publisher"))
        if publisher:
            catalog.append("Publisher: " + publisher)
        spatial = (item.get("spatial") or "").strip()
        if spatial:
            catalog.append("Spatial: " + spatial)
        lic = (item.get("license") or "").strip()
        if lic:
            catalog.append("License: " + lic)
        is_part_of = (item.get("isPartOf") or "").strip()
        if is_part_of:
            catalog.append("Part of: " + is_part_of)
        if catalog:
            parts.append(" | ".join(catalog))

        return "\n\n".join(parts)

    @staticmethod
    def _parse_date(raw: str) -> str:
        if not raw:
            return ""
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if m:
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60

        for page in range(self._MAX_PAGES):
            # Wall-clock budget
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached at page {page}. Exiting.")
                break

            if limit is not None and saved >= limit:
                break

            offset = page * self._PAGE_SIZE
            url = f"{self._SEARCH_API}?fulltext=&rows={self._PAGE_SIZE}&start={offset}"

            raw = self._curl_get(url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch page {page} (start={offset}). Stopping.")
                break

            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[{self.site_id}] JSON error at page {page}: {exc}")
                break

            results = data.get("results") or {}
            if not results:
                print(f"[{self.site_id}] No results at page {page} (start={offset}). Done.")
                break

            try:
                total = int(data.get("total") or 0)
            except (ValueError, TypeError):
                total = 0

            if page % 10 == 0:
                lim_s = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_s}, total_on_server={total}")

            for key, item in results.items():
                if limit is not None and saved >= limit:
                    break

                try:
                    identifier = (item.get("identifier") or "").strip()
                    if not identifier:
                        continue

                    detail_url = f"{self.base_url}/dataset/{identifier}"
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    title = (item.get("title") or "").strip()
                    if not title:
                        continue

                    abstract = self._build_abstract(item, title, identifier)
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping (abstract {len(abstract)} chars): {title[:60]}")
                        continue

                    published_date = self._parse_date(item.get("issued") or "")
                    listed_date = self._parse_date(item.get("modified") or "")

                    publisher = self._extract_publisher(item.get("publisher"))

                    kw_list = self._extract_list_field(item.get("keyword"))
                    keywords = ", ".join(kw_list) if kw_list else ""

                    theme_list = self._extract_list_field(item.get("theme"))
                    category = "; ".join(theme_list) if theme_list else ""

                    # First PDF distribution
                    pdf_url = None
                    original_filename = None
                    for dist in (item.get("distribution") or []):
                        if not isinstance(dist, dict):
                            continue
                        dl_url = (dist.get("downloadURL") or "").strip()
                        mime = (dist.get("mediaType") or dist.get("format") or "").lower()
                        if not dl_url:
                            for ref in (dist.get("%Ref:downloadURL") or []):
                                if isinstance(ref, dict):
                                    fp = (ref.get("data") or {}).get("filePath", "")
                                    if fp:
                                        dl_url = fp
                                        break
                        if dl_url and ("pdf" in mime or dl_url.lower().endswith(".pdf")):
                            pdf_url = dl_url
                            original_filename = dl_url.rstrip("/").split("/")[-1].split("?")[0]
                            break

                    contact = item.get("contactPoint") or {}
                    dept = contact.get("fn", "") if isinstance(contact, dict) else ""

                    metadata = {
                        "posted_date": item.get("modified") or "",
                        "originalFilename": original_filename,
                        "accessLevel": item.get("accessLevel"),
                        "accrualPeriodicity": item.get("accrualPeriodicity"),
                        "license": item.get("license"),
                        "spatial": item.get("spatial"),
                        "isPartOf": item.get("isPartOf"),
                        "contactPoint": item.get("contactPoint"),
                        "distribution_count": len(item.get("distribution") or []),
                        "identifier": identifier,
                        "category": category,
                    }

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": identifier,
                        "post_number": identifier,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "authors": "",
                        "publisher": publisher,
                        "department": dept,
                        "keywords": keywords,
                        "category": category,
                        "doi": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    lim_s = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] Saved {saved}/{lim_s}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {key} failed: {exc}")
                    continue

            # End-of-results check
            if total and (offset + self._PAGE_SIZE) >= total:
                print(f"[{self.site_id}] Reached end (offset {offset + self._PAGE_SIZE} >= total {total}). Done.")
                break

            if page == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")

            time.sleep(0.5)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
