# -*- coding: utf-8 -*-
"""NIST Science Data Portal (data.nist.gov/sdp) crawler.

API: https://data.nist.gov/rmm/records?size=N&page=P  (0-indexed pages)
Total: ~1654 records (all dataset metadata, no per-item detail fetch needed).
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse

# spec_from_file_location has no package context — use absolute import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler

_SITE_ID = "data-nist-gov-sdp"
_API_URL = "https://data.nist.gov/rmm/records"
_PAGE_SIZE = 50
_MAX_PAGES = 200
_WALL_BUDGET_S = 25 * 60  # 25 minutes


class NistSdpCrawler(BaseCrawler):
    """Crawler for NIST Science Data Portal."""

    site_id = "data-nist-gov-sdp"
    site_name = "Custom: data-nist-gov-sdp"
    base_url = "https://data.nist.gov"

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict = None) -> str | None:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
                body = result.stdout
                if body and body.strip():
                    return body
            except Exception as exc:
                print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/3): {exc}")
            wait = (attempt + 1) ** 2  # 1s, 4s, 9s
            if attempt < 2:
                print(f"[{_SITE_ID}] Empty/error response, retrying in {wait}s...")
                time.sleep(wait)
        return None

    def _fetch_page(self, page: int) -> dict | None:
        for attempt in range(3):
            raw = self._curl_get(_API_URL, {"size": _PAGE_SIZE, "page": page})
            if raw:
                try:
                    return json.loads(raw)
                except json.JSONDecodeError as exc:
                    print(f"[{_SITE_ID}] JSON error at page {page}: {exc}")
            wait = (attempt + 1) * 3
            if attempt < 2:
                print(f"[{_SITE_ID}] Fetch failed page {page} (attempt {attempt + 1}/3), retrying in {wait}s...")
                time.sleep(wait)
        print(f"[{_SITE_ID}] Giving up on page {page} after 3 attempts.")
        return None

    # ------------------------------------------------------------------
    # Field extraction helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _flatten(val) -> str:
        """Join list → str, or coerce to str."""
        if isinstance(val, list):
            return " ".join(str(v).strip() for v in val if v)
        return str(val).strip() if val else ""

    @staticmethod
    def _extract_pdf(components: list) -> tuple[str, str]:
        """Return (pdf_url, original_filename) from components list."""
        for c in (components or []):
            dl = (c.get("downloadURL") or "").strip()
            mt = (c.get("mediaType") or "").lower()
            if dl and ("pdf" in mt or dl.lower().endswith(".pdf")):
                fname = urllib.parse.unquote(dl.rstrip("/").split("/")[-1].split("?")[0])
                return dl, fname
        return "", ""

    @staticmethod
    def _extract_doi(item: dict) -> str:
        for ref in (item.get("references") or []):
            loc = ref.get("location") or ""
            if "doi.org" in loc:
                return loc
        for c in (item.get("components") or []):
            acc = c.get("accessURL") or ""
            if "doi.org" in acc:
                return acc
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.monotonic()
        limit_label = str(limit) if limit is not None else "∞"

        for page in range(_MAX_PAGES):
            # Wall-clock budget
            elapsed = time.monotonic() - start_time
            if elapsed > _WALL_BUDGET_S:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded ({elapsed:.0f}s). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            time.sleep(self._delay)

            data = self._fetch_page(page)
            if data is None:
                print(f"[{_SITE_ID}] Page {page} failed permanently. Stopping.")
                break

            items = data.get("ResultData") or []
            if not items:
                print(f"[{_SITE_ID}] No items on page {page}. Done.")
                break

            if page == 0:
                total = data.get("ResultCount", "?")
                print(f"[{_SITE_ID}] Total records on server: {total}")

            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_label}")

            page_had_new = False

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    ark_id = (item.get("@id") or "").strip()
                    ediid = (item.get("ediid") or "").strip()
                    title = (item.get("title") or "").strip()

                    if not title:
                        continue

                    # Detail URL (SDP angular fragment URL)
                    if ark_id:
                        detail_url = f"https://data.nist.gov/sdp/#/id/{ark_id}"
                    else:
                        detail_url = f"https://data.nist.gov/od/id/{ediid}"

                    # URL dedup
                    url_key = detail_url or ediid or ark_id
                    if url_key in seen_urls:
                        continue
                    seen_urls.add(url_key)
                    page_had_new = True

                    # Abstract
                    abstract = self._flatten(item.get("description") or "")
                    if len(abstract) < 50:
                        print(f"[{_SITE_ID}] Skipping '{title[:50]}' — abstract too short ({len(abstract)} chars)")
                        continue

                    # Dates
                    modified = (item.get("modified") or "").strip()
                    pub_date = modified[:10] if len(modified) >= 10 else modified

                    # Keywords — each element may itself be semicolon-joined
                    kw_parts: list[str] = []
                    for kw in (item.get("keyword") or []):
                        kw_parts.extend(k.strip() for k in str(kw).split(";") if k.strip())
                    keywords = ",".join(kw_parts)

                    # Category from theme
                    theme = item.get("theme") or []
                    category = "; ".join(str(t).strip() for t in theme if t)

                    # Publisher
                    pub_raw = item.get("publisher") or {}
                    publisher = pub_raw.get("name", "") if isinstance(pub_raw, dict) else str(pub_raw)

                    # Author from contactPoint
                    cp = item.get("contactPoint") or {}
                    authors = cp.get("fn", "") if isinstance(cp, dict) else ""

                    # PDF + filename
                    components = item.get("components") or []
                    pdf_url, original_filename = self._extract_pdf(components)

                    # DOI
                    doi = self._extract_doi(item)

                    # Metadata: all raw fields not mapped to top-level columns
                    metadata_dict = {
                        "posted_date": modified,
                        "ark_id": ark_id,
                        "ediid": ediid,
                        "status": item.get("status") or "",
                        "accessLevel": item.get("accessLevel") or "",
                        "license": item.get("license") or "",
                        "bureauCode": item.get("bureauCode") or [],
                        "programCode": item.get("programCode") or [],
                        "version": item.get("version") or "",
                        "landingPage": item.get("landingPage") or "",
                        "language": item.get("language") or [],
                        "topic": [
                            t.get("tag", "") for t in (item.get("topic") or [])
                            if isinstance(t, dict) and t.get("tag")
                        ],
                    }
                    # Drop empty values to keep metadata lean
                    metadata_dict = {k: v for k, v in metadata_dict.items()
                                     if v not in (None, "", [], {})}

                    paper = {
                        "site_id": self.site_id,
                        "external_id": ediid or ark_id,
                        "post_number": ediid or ark_id or None,
                        "title": title,
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "authors": authors or None,
                        "publisher": publisher or None,
                        "url": detail_url,
                        "pdf_url": pdf_url or None,
                        "keywords": keywords or None,
                        "category": category or None,
                        "doi": doi or None,
                        "original_filename": original_filename or None,
                        "metadata": json.dumps(metadata_dict, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{_SITE_ID}] Saved {saved}/{limit_label}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] Item '{(item.get('title') or '')[:40]}' failed: {exc}")
                    continue

            # If this page had no new URLs, the paginator may be looping — stop
            if not page_had_new and items:
                print(f"[{_SITE_ID}] No new URLs on page {page}. Stopping to avoid loop.")
                break

        if page >= _MAX_PAGES - 1:
            print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached.")

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
