# -*- coding: utf-8 -*-
"""Crawler for data.biodiversity.be — Belgian biodiversity data portal (CKAN 2.9.2).

API: https://data.biodiversity.be/api/3/action/package_search
549 datasets; all metadata returned in the list call — no per-item fetches needed.
"""

import json
import os
import sys
import time

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler


class DataBiodiversityBeCrawler(BaseCrawler):
    site_id = "data-biodiversity-be-dataset"
    site_name = "Custom: data-biodiversity-be-dataset"
    base_url = "https://data.biodiversity.be"

    _API_BASE = "https://data.biodiversity.be/api/3/action"
    _PAGE_SIZE = 100
    _ABSTRACT_MIN = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _BUDGET_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

    def _solve_anubis(self) -> bool:
        """Solve the site's Anubis proof-of-work JS challenge once via a
        real headless browser, then copy the resulting auth cookies into
        self._session so plain HTTP API calls (curl/requests) pass through
        for the rest of the crawl without re-solving.
        """
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                ctx = browser.new_context(
                    user_agent=self.USER_AGENT, viewport={"width": 1920, "height": 1080}
                )
                page = ctx.new_page()
                page.goto(self.base_url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(6000)
                cookies = ctx.cookies()
                browser.close()
            for c in cookies:
                self._session.cookies.set(c["name"], c["value"], domain=c["domain"])
            print(f"[{self.site_id}] Anubis challenge solved ({len(cookies)} cookies)")
            return True
        except Exception as exc:
            print(f"[{self.site_id}] Anubis challenge solve failed: {exc}")
            return False

    def crawl(self, limit=None):
        saved = 0
        seen_ids = set()
        limit_str = str(limit) if limit is not None else "inf"
        t0 = time.time()

        self._solve_anubis()

        for page in range(self._MAX_PAGES):
            if time.time() - t0 > self._BUDGET_SECS:
                print(f"[{self.site_id}] 25-min budget reached, stopping at {saved} saves.")
                break
            if limit is not None and saved >= limit:
                break

            offset = page * self._PAGE_SIZE
            resp = self._fetch_api(
                f"{self._API_BASE}/package_search",
                {"rows": self._PAGE_SIZE, "start": offset, "sort": "metadata_created asc"},
            )
            if resp is None:
                print(f"[{self.site_id}] page {page}: fetch failed after retries, stopping.")
                break

            try:
                data = resp.json()
            except Exception as exc:
                print(f"[{self.site_id}] page {page}: JSON parse error: {exc}")
                break

            if not data.get("success"):
                print(f"[{self.site_id}] page {page}: API returned success=false")
                break

            results = (data.get("result") or {}).get("results") or []
            if not results:
                print(f"[{self.site_id}] page {page}: empty results, done.")
                break

            new_this_page = 0
            for item in results:
                if limit is not None and saved >= limit:
                    break
                try:
                    pkg_id = item.get("id") or item.get("name")
                    if not pkg_id or pkg_id in seen_ids:
                        continue
                    seen_ids.add(pkg_id)

                    abstract = (item.get("notes") or "").strip()
                    if len(abstract) < self._ABSTRACT_MIN:
                        print(
                            f"[{self.site_id}] skip '{item.get('title')}': "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    slug = item.get("name") or pkg_id
                    detail_url = f"{self.base_url}/dataset/{slug}"
                    published_date = _parse_date(item.get("metadata_created"))
                    listed_date = _parse_date(item.get("metadata_modified"))

                    author = (item.get("author") or "").strip() or None
                    org = item.get("organization") or {}
                    publisher = (org.get("title") or "").strip() or None

                    tags = item.get("tags") or []
                    keywords = ",".join(t["name"] for t in tags if t.get("name")) or None
                    category = (item.get("dataset_type") or "").strip() or None

                    # Prefer PDF resource; fall back to DWC-A archive URL
                    pdf_url = None
                    original_filename = None
                    for res in (item.get("resources") or []):
                        res_url = (res.get("url") or "").strip()
                        if not res_url:
                            continue
                        fmt = (res.get("format") or "").upper()
                        if fmt == "PDF":
                            pdf_url = res_url
                            original_filename = _filename_from_url(res_url)
                            break
                    if not pdf_url and item.get("dwca_url"):
                        pdf_url = item["dwca_url"]
                        original_filename = _filename_from_url(pdf_url)

                    metadata = {
                        "posted_date": item.get("metadata_modified"),
                        "gbif_uuid": item.get("gbif_uuid"),
                        "dwca_url": item.get("dwca_url"),
                        "dataset_type": category,
                        "license_id": item.get("license_id"),
                        "license_title": item.get("license_title"),
                        "license_url": item.get("license_url"),
                        "dataset_website": item.get("dataset_website") or None,
                        "ckan_name": slug,
                        "num_resources": item.get("num_resources"),
                        "isopen": item.get("isopen"),
                        "administrative_contact": item.get("administrative_contact") or None,
                        "metadata_contact": item.get("metadata_contact") or None,
                    }

                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": pkg_id,
                        "post_number": slug,
                        "title": (item.get("title") or "(untitled)").strip(),
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": listed_date,
                        "authors": author,
                        "publisher": publisher,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "keywords": keywords,
                        "category": category,
                        "original_filename": original_filename,
                        "metadata": json.dumps(metadata, ensure_ascii=False),
                    })
                    saved += 1
                    new_this_page += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{item.get('name', '?')}' failed: {exc}")
                    continue

            if (page + 1) % 10 == 0:
                print(f"[{self.site_id}] page {page + 1}: saved {saved}/{limit_str}")

            if new_this_page == 0 and page > 0:
                print(f"[{self.site_id}] page {page}: no new items on page, done.")
                break

            if page == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached.")

        return saved

    def _fetch_api(self, url, params, retries=3):
        for attempt in range(retries):
            if attempt > 0:
                wait = 3 ** attempt  # 3s, 9s
                print(f"[{self.site_id}] retrying in {wait}s... (attempt {attempt + 1}/{retries})")
                time.sleep(wait)
            try:
                resp = self._session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                return resp
            except Exception as exc:
                print(f"[{self.site_id}] fetch error (attempt {attempt + 1}/{retries}): {exc}")
        return None


def _parse_date(s):
    if not s:
        return None
    return s[:10]  # YYYY-MM-DD from ISO datetime


def _filename_from_url(url):
    if not url:
        return None
    tail = url.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
    return tail if tail else None
