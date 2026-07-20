# -*- coding: utf-8 -*-
"""Crawler for Central Bank of Ireland Open Data Portal (CKAN 2.9.14).

Starting URL: https://opendata.centralbank.ie/dataset/
API: CKAN 3 JSON API — /api/3/action/package_search
WAF: AWS WAF JS challenge — requires Playwright (headless Chromium) for fetching.

Usage (absolute import via spec_from_file_location):
    sys.path.insert(0, '.')
    from crawler.base_crawler import BaseCrawler  # loaded first so relative imports inside work
"""

import json
import re
import sys
import time
from typing import Optional

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_SITE_ID = "opendata-centralbank-ie-dataset"
_BASE_URL = "https://opendata.centralbank.ie"
_PAGE_SIZE = 20
_WALL_CLOCK_LIMIT = 25 * 60  # seconds


class OpendataCentralbankIeDatasetCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: opendata-centralbank-ie-dataset"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl CKAN datasets from Central Bank of Ireland Open Data Portal.

        Uses CKAN package_search API with pagination. Each CKAN package = one document.
        AWS WAF JS challenge is bypassed via Playwright (headless Chromium).
        """
        start_time = time.time()
        saved = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "∞"

        page = 0
        while True:
            # Wall-clock budget
            if time.time() - start_time > _WALL_CLOCK_LIMIT:
                print(f"[{_SITE_ID}] Wall-clock budget exceeded, stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page >= 200:
                print(f"[{_SITE_ID}] Safety cap of 200 pages reached, stopping.")
                break

            if page > 0 and page % 10 == 0:
                print(f"[{_SITE_ID}] page {page}: saved {saved}/{limit_str}")

            start_offset = page * _PAGE_SIZE
            api_url = (
                f"{_BASE_URL}/api/3/action/package_search"
                f"?rows={_PAGE_SIZE}&start={start_offset}&sort=metadata_created+desc"
            )

            data = self._fetch_json(api_url)
            if data is None:
                print(f"[{_SITE_ID}] Failed to fetch page {page}, stopping.")
                break

            results = data.get("result", {}).get("results", [])
            total = data.get("result", {}).get("count", 0)

            if not results:
                print(f"[{_SITE_ID}] No results on page {page}, stopping.")
                break

            new_on_page = 0
            for pkg in results:
                if limit is not None and saved >= limit:
                    break

                item_url = f"{_BASE_URL}/dataset/{pkg.get('name', '')}"
                if item_url in seen_urls:
                    continue
                seen_urls.add(item_url)

                try:
                    paper = self._build_paper(pkg)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    new_on_page += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{_SITE_ID}] item {pkg.get('name', '?')} failed: {exc}")
                    continue

            # End-of-data: exhausted all results or got a partial page
            if start_offset + len(results) >= total:
                break
            if new_on_page == 0 and len(results) < _PAGE_SIZE:
                break

            page += 1
            time.sleep(1.0)  # polite rate-limit between pages

        print(f"[{_SITE_ID}] Done: saved {saved} documents.")
        return saved

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fetch_json(self, url: str, retries: int = 3) -> Optional[dict]:
        """Fetch a CKAN API URL via Playwright and return parsed JSON.

        Playwright handles the AWS WAF JavaScript challenge that blocks plain curl/requests.
        Chrome renders JSON URLs as <pre> text, so we extract from that element.
        Retries with exponential back-off: 1s, 3s, 9s.
        """
        from crawler.playwright_fetcher import fetch_html

        delays = [1, 3, 9]
        for attempt in range(retries):
            if attempt > 0:
                wait = delays[min(attempt - 1, len(delays) - 1)]
                print(f"[{_SITE_ID}] retry {attempt}/{retries} in {wait}s for {url}")
                time.sleep(wait)
            try:
                html = fetch_html(
                    url,
                    timeout_seconds=30,
                    extra_wait_seconds=2.0,
                    block_resources=True,
                )
                if not html:
                    continue

                text = _extract_json_text(html)
                if not text:
                    print(f"[{_SITE_ID}] No JSON found in response for {url}")
                    continue

                return json.loads(text)

            except json.JSONDecodeError as exc:
                print(f"[{_SITE_ID}] JSON parse error attempt {attempt + 1}: {exc}")
            except Exception as exc:
                print(f"[{_SITE_ID}] _fetch_json error attempt {attempt + 1}: {exc}")

        return None

    def _build_paper(self, pkg: dict) -> Optional[dict]:
        """Build paper dict from a CKAN package record."""
        # Abstract — prefer English notes_translated, fallback to notes
        abstract = (
            ((pkg.get("notes_translated") or {}).get("en") or "").strip()
            or (pkg.get("notes") or "").strip()
        )

        if len(abstract) < 50:
            print(
                f"[{_SITE_ID}] Skipping {pkg.get('name')}: "
                f"abstract too short ({len(abstract)} chars)"
            )
            return None

        # Title
        title = (
            ((pkg.get("title_translated") or {}).get("en") or "").strip()
            or (pkg.get("title") or "").strip()
            or pkg.get("name", "")
        )

        # Dates
        published_date = _parse_date(pkg.get("issued") or pkg.get("metadata_created"))
        posted_date = _parse_date(pkg.get("metadata_created"))

        # Publisher from org
        org = pkg.get("organization") or {}
        publisher = (
            ((org.get("title_translated") or {}).get("en") or "").strip()
            or (org.get("title") or "").strip()
            or (org.get("name") or "").strip()
            or "Central Bank of Ireland"
        )

        # Tags → keywords (comma-separated)
        tags = [t.get("name", "").strip() for t in (pkg.get("tags") or []) if t.get("name")]
        keywords = ", ".join(tags) if tags else None

        # Category from theme or groups
        theme = (pkg.get("theme") or "").strip()
        groups = pkg.get("groups") or []
        category = theme or ((groups[0].get("title") or "").strip() if groups else "") or None

        # Author
        author = (pkg.get("author") or pkg.get("contact_name") or "").strip() or None

        # PDF resource — CKAN resources are mostly CSV; scan for PDF just in case
        pdf_url = None
        original_filename = None
        for res in pkg.get("resources") or []:
            if (res.get("format") or "").upper() == "PDF":
                candidate = (res.get("url") or "").strip()
                if candidate:
                    pdf_url = candidate
                    tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                    original_filename = tail if "." in tail else None
                break

        item_url = f"{_BASE_URL}/dataset/{pkg['name']}"

        metadata = {
            "node_id": pkg.get("id"),
            "name": pkg.get("name"),
            "frequency": pkg.get("frequency"),
            "language": pkg.get("language"),
            "license_id": pkg.get("license_id"),
            "license_title": pkg.get("license_title"),
            "temporal": pkg.get("temporal"),
            "temporal_start": pkg.get("temporal_start"),
            "temporal_end": pkg.get("temporal_end"),
            "contact_email": pkg.get("contact_email"),
            "contact_name": pkg.get("contact_name"),
            "conforms_to": pkg.get("conforms_to"),
            "num_resources": pkg.get("num_resources"),
            "resource_formats": [r.get("format") for r in (pkg.get("resources") or [])],
            "metadata_modified": pkg.get("metadata_modified"),
            "posted_date": pkg.get("metadata_created"),
            "originalFilename": original_filename,
        }

        return {
            "site_id": _SITE_ID,
            "external_id": pkg.get("id", ""),
            "post_number": pkg["name"],
            "url": item_url,
            "title": title,
            "abstract": abstract,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "published_date": published_date,
            "posted_date": posted_date,
            "authors": author,
            "publisher": publisher,
            "department": None,
            "journal": None,
            "keywords": keywords,
            "category": category,
            "doi": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }


# ------------------------------------------------------------------
# Module-level helpers
# ------------------------------------------------------------------

def _extract_json_text(html: str) -> Optional[str]:
    """Extract JSON text from Playwright-rendered HTML of a JSON API response.

    Chrome renders JSON URLs as <pre> text. Falls back to regex extraction
    targeting the CKAN-specific ``{"help":`` opener.
    """
    # Primary: BeautifulSoup <pre> extraction
    try:
        try:
            from bs4 import BeautifulSoup as _BS
            soup = _BS(html, "html5lib")
        except Exception:
            try:
                from bs4 import BeautifulSoup as _BS
                soup = _BS(html, "lxml")
            except Exception:
                from bs4 import BeautifulSoup as _BS
                soup = _BS(html, "html.parser")

        pre = soup.find("pre")
        if pre:
            text = pre.get_text()
            if text and text.strip().startswith("{"):
                return text.strip()
    except Exception:
        pass

    # Fallback: find CKAN API JSON marker and extract via balanced-brace scan
    m = re.search(r'\{"help"\s*:', html)
    if m:
        candidate = html[m.start():]
        # Find the matching closing brace using a simple balance counter
        depth = 0
        in_str = False
        escape = False
        for i, ch in enumerate(candidate):
            if escape:
                escape = False
                continue
            if ch == "\\" and in_str:
                escape = True
                continue
            if ch == '"' and not escape:
                in_str = not in_str
                continue
            if in_str:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return candidate[: i + 1]

    return None


def _parse_date(date_str) -> Optional[str]:
    """Return ISO YYYY-MM-DD from any date/datetime string, or None."""
    if not date_str:
        return None
    s = str(date_str).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None
