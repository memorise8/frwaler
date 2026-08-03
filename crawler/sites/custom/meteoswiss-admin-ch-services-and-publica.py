# -*- coding: utf-8 -*-
"""MeteoSwiss publications crawler.

Target:  https://www.meteoswiss.admin.ch/services-and-publications/publications.html

Discovery: API endpoint found via Playwright network interception.
  List API:   /api/search/public-en/publications/results.json?start=N&sort=newest
  Page size:  fixed 12 per page (rows param ignored by server)
  Total:      ~1567 publications
  Detail page: HTML attributes (authors, publication-src, edition) on web components
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler


class MeteoSwissPublicationsCrawler(BaseCrawler):

    site_id   = "meteoswiss-admin-ch-services-and-publica"
    site_name = "Custom: meteoswiss-admin-ch-services-and-publica"
    base_url  = "https://www.meteoswiss.admin.ch"

    _LIST_API  = "https://www.meteoswiss.admin.ch/api/search/public-en/publications/results.json"
    _PAGE_SIZE = 12   # API is hard-capped at 12 items per page
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    # ------------------------------------------------------------------ #
    # Network helpers
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str, *, compressed: bool = True) -> str | None:
        """GET via curl with up to 3 retries (1s → 3s → 9s backoff)."""
        cmd = [
            "curl", "-sk", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/html, */*",
            "-H", "Referer: https://www.meteoswiss.admin.ch/services-and-publications/publications.html",
        ]
        if compressed:
            cmd.append("--compressed")
        cmd.append(url)

        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt + 1}/3) for {url}: {exc}")
            if attempt < 2:
                time.sleep([1, 3, 9][attempt])
        return None

    def _fetch_list_page(self, start: int) -> dict | None:
        """Fetch one page of the publications search API."""
        url = f"{self._LIST_API}?start={start}&sort=newest"
        raw = self._curl_get(url, compressed=True)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] JSON decode error at start={start}: {exc}")
            return None

    def _fetch_detail(self, path: str) -> dict:
        """Fetch a publication detail page and extract authors, PDF URL, edition."""
        url = f"{self.base_url}{path}"
        html = self._curl_get(url, compressed=False)
        if not html:
            return {}

        result: dict = {}

        # --- authors attribute on the mch-publication-page web component ---
        m = re.search(r'\bauthors="([^"]+)"', html)
        if m:
            result["authors"] = m.group(1).replace("&quot;", '"')

        # --- publication-src JSON attribute → PDF href + label ---
        m = re.search(r'\bpublication-src="([^"]+)"', html)
        if m:
            try:
                raw_json = m.group(1).replace("&quot;", '"').replace("&amp;", "&")
                pub_src = json.loads(raw_json)
                href = pub_src.get("href", "")
                if href and href.lower().endswith(".pdf"):
                    result["pdf_url"] = (
                        f"{self.base_url}{href}" if href.startswith("/") else href
                    )
                    result["original_filename"] = href.rstrip("/").split("/")[-1]
                result["pdf_label"] = pub_src.get("label", "")
            except Exception:
                pass

        # --- edition attribute (journal / report-series name) ---
        m = re.search(r'\bedition="([^"]+)"', html)
        if m:
            result["edition"] = m.group(1).replace("&quot;", '"')

        return result

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        saved        = 0
        start        = 0
        page         = 1
        seen_urls: set = set()
        start_time   = time.time()
        MAX_WALL     = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        limit_display = str(limit) if limit is not None else "inf"

        while True:
            # ---- budget / termination guards ----
            if time.time() - start_time > MAX_WALL:
                print(f"[{self.site_id}] Wall-clock budget reached (25 min). Stopping.")
                break

            if limit is not None and saved >= limit:
                break

            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                break

            # ---- fetch list page ----
            data = None
            for retry in range(3):
                data = self._fetch_list_page(start)
                if data:
                    break
                wait = [1, 3, 9][retry]
                print(f"[{self.site_id}] List retry {retry + 1}/3 at start={start}, "
                      f"waiting {wait}s…")
                time.sleep(wait)

            if data is None:
                print(f"[{self.site_id}] Failed to fetch list at start={start}. Stopping.")
                break

            docs  = data.get("response", {}).get("docs", [])
            total = data.get("response", {}).get("numFound", 0)

            if not docs:
                print(f"[{self.site_id}] No more docs at start={start}. Done.")
                break

            if page == 1:
                print(f"[{self.site_id}] Total publications on server: {total}")

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            new_this_page = 0

            for doc in docs:
                if limit is not None and saved >= limit:
                    break

                path = doc.get("path", "")
                if not path:
                    continue

                detail_url = f"{self.base_url}{path}"

                # URL deduplication (prevents infinite loops on paginator glitch)
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_this_page += 1

                try:
                    title    = (doc.get("title") or "").strip()
                    abstract = (doc.get("lead") or doc.get("description") or "").strip()

                    if len(abstract) < 100:
                        print(f"[{self.site_id}] Skipping short abstract "
                              f"({len(abstract)} chars): {title[:60]}")
                        continue

                    pub_date_raw = doc.get("publicationDate", "") or ""
                    pub_date     = pub_date_raw[:10] if pub_date_raw else ""  # YYYY-MM-DD

                    keywords_raw = doc.get("keywords", "") or ""
                    categories   = doc.get("categories", []) or []
                    page_sub     = doc.get("pageSubType", "") or ""
                    version      = doc.get("_version_", "")
                    post_number  = str(version) if version else None

                    # ---- detail page fetch ----
                    time.sleep(self._delay)
                    detail: dict = {}
                    try:
                        detail = self._fetch_detail(path)
                    except Exception as exc:
                        print(f"[{self.site_id}] Detail fetch failed for {path}: {exc}")

                    # authors: "Alice, Bob, Carol" → "Alice; Bob; Carol"
                    authors_raw = detail.get("authors", "") or ""
                    authors = (
                        "; ".join(a.strip() for a in authors_raw.split(",") if a.strip())
                        if authors_raw else ""
                    )

                    pdf_url           = detail.get("pdf_url", "") or ""
                    original_filename = detail.get("original_filename", "") or ""
                    edition           = detail.get("edition", "") or ""
                    pdf_label         = detail.get("pdf_label", "") or ""

                    metadata: dict = {
                        "pageSubType":      page_sub,
                        "publicationYear":  doc.get("publicationYear", "") or "",
                        "modificationDate": doc.get("modificationDate", "") or "",
                        "categoriesText":   doc.get("categoriesText", []) or [],
                        "version":          str(version) if version else "",
                    }
                    if edition:
                        metadata["edition"] = edition
                    if pdf_label:
                        metadata["originalFilename"] = pdf_label

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":        path,
                        "post_number":        post_number,
                        "title":              title,
                        "abstract":           abstract,
                        "published_date":     pub_date,
                        "listed_date":        pub_date,
                        "url":                detail_url,
                        "pdf_url":            pdf_url,
                        "authors":            authors,
                        "publisher":          "MeteoSwiss",
                        "journal":            edition,
                        "keywords":           keywords_raw,
                        "category":           "; ".join(categories),
                        "original_filename":  original_filename,
                        "metadata":           json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_display}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {path} failed: {exc}")
                    continue

            # end-of-pagination detection
            if new_this_page == 0:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break

            start += self._PAGE_SIZE
            page  += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
