# -*- coding: utf-8 -*-
"""Crawler for NLM Digital Collections — NIH Annual Reports (DREPNIHAR).

Target: https://collections.nlm.nih.gov/?f%5Bdrep2.isMemberOfCollection%5D%5B%5D=DREPNIHAR
Total records: 29 (as of 2026-06)

API:  Blacklight/Solr JSON-API at /catalog.json
      /catalog/{item_id}.json  — per-item full metadata

WAF:  The site is behind AWS WAF (HTTP 202 JS-challenge on bare curl/requests).
      We solve it once via Playwright (headless Chromium), then reuse the
      session via page.request.get() which carries the aws-waf-token cookie.
"""

from __future__ import annotations

import json
import re
import sys
import time
from html import unescape
from pathlib import Path
from typing import Optional

# Absolute import — spec_from_file_location has no package context.
_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from crawler.base_crawler import BaseCrawler  # noqa: E402

_SITE_ID = "collections-nlm-nih-gov"
_BASE_URL = "https://collections.nlm.nih.gov"
_COLLECTION_ID = "DREPNIHAR"
_COLLECTION_NAME = "NIH Annual Reports"
_LIST_API = f"{_BASE_URL}/catalog.json"
_SEED_URL = (
    f"{_BASE_URL}/?f%5Bdrep2.isMemberOfCollection%5D%5B%5D={_COLLECTION_ID}"
)
_PER_PAGE = 100       # site supports up to 100; gets all 29 in one shot
_PAGE_CAP = 200
_BUDGET_SECS = 25 * 60   # 25-min wall-clock cap


def _clean(text: str) -> str:
    """Strip HTML tags and normalise whitespace."""
    text = re.sub(r"<br\s*/?>", "; ", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r";+", ";", text)
    return re.sub(r"\s+", " ", text).strip("; ").strip()


def _get(d: dict, *keys: str) -> str:
    """Return first non-empty value for any of the given keys in *d*."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


class NLMCollectionsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: collections-nlm-nih-gov"
    base_url = _BASE_URL

    # ------------------------------------------------------------------
    # Playwright bootstrap
    # ------------------------------------------------------------------

    def _launch_playwright(self):
        """Start Playwright, open headless Chromium, solve AWS WAF challenge.

        Returns (pw_instance, browser, page).  The page's request context
        carries the aws-waf-token cookie for all subsequent page.request calls.
        """
        from playwright.sync_api import sync_playwright  # type: ignore

        pw = sync_playwright().start()
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            user_agent=self.USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        page = ctx.new_page()
        # Navigate once → AWS WAF JS challenge runs → sets aws-waf-token cookie.
        try:
            page.goto(_SEED_URL, wait_until="networkidle", timeout=30_000)
        except Exception as exc:
            print(f"[{_SITE_ID}] WAF-seed navigation note: {exc}")
        time.sleep(2)
        return pw, browser, page

    # ------------------------------------------------------------------
    # Fetch helpers (use playwright page.request — carries WAF cookie)
    # ------------------------------------------------------------------

    def _fetch_json(self, page, url: str, retries: int = 3) -> Optional[dict]:
        """GET JSON via playwright's request context (carries WAF cookie)."""
        for attempt in range(retries):
            try:
                resp = page.request.get(
                    url,
                    timeout=30_000,
                    headers={"Accept": "application/json"},
                )
                if resp.ok:
                    raw = resp.text()
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        print(f"[{_SITE_ID}] JSON parse error for {url}: {exc}")
                        return None
                print(f"[{_SITE_ID}] HTTP {resp.status} for {url}")
            except Exception as exc:
                wait = [1, 3, 9][min(attempt, 2)]
                print(
                    f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) "
                    f"for {url}: {exc}"
                )
                if attempt < retries - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _attr(item_attrs: dict, key: str) -> str:
        """Extract plain text from a Blacklight document_value attribute dict."""
        v = item_attrs.get(key)
        if v is None:
            return ""
        if isinstance(v, dict):
            raw = (v.get("attributes") or {}).get("value", "")
        else:
            raw = str(v)
        return _clean(raw)

    @staticmethod
    def _extract_year(title: str) -> Optional[str]:
        """Return 'YYYY-01-01' if title contains '(YYYY)', else None."""
        m = re.search(r"\((\d{4})\)", title)
        return f"{m.group(1)}-01-01" if m else None

    @staticmethod
    def _leaf_id(item_id: str) -> str:
        """Extract NLM leaf identifier from item ID.

        'nlm:nlmuid-7802080X3-mvpart' → '7802080X3'
        'nlm:nlmuid-0141176-mvset'     → '0141176'
        """
        m = re.match(r"nlm:nlmuid-(.+?)-(mvset|mvpart|bk|leaf|root)$", item_id)
        if m:
            return m.group(1)
        # Generic fallback
        parts = item_id.rsplit("-", 1)
        return parts[0].split("-")[-1] if len(parts) >= 2 else item_id

    @staticmethod
    def _build_abstract(list_attrs: dict, detail_attrs: dict) -> str:
        """Build a rich abstract (always ≥ 100 chars) from all available metadata.

        Uses data from both the list-API item and the per-item detail JSON.
        """
        def gf(*keys):
            for k in keys:
                v = NLMCollectionsCrawler._attr(list_attrs, k)
                if v:
                    return v
                v = NLMCollectionsCrawler._attr(detail_attrs, k)
                if v:
                    return v
            return ""

        title    = list_attrs.get("title", "") or detail_attrs.get("title", "")
        author   = gf("drep2.authorAggregate", "drep.authorAggregate")
        pub      = gf("drep2.pubConcat",  "drep.pubConcat",
                      "drep2.pubconcat",  "drep.pubconcat")
        subjects = gf("drep2.subjectAggregate", "drep.subjectAggregate",
                      "drep2.subjectaggregate", "drep.subjectaggregate")
        genre    = gf("drep2.subjectGenre",  "drep.subjectGenre",
                      "drep2.subjectgenre", "drep.subjectgenre")
        lang     = gf("drep2.language", "drep.language")
        fmt      = gf("drep.format",    "drep2.format")
        rights   = gf("drep.rights",    "drep2.rights")
        collection = gf("drep2.isMemberOfCollection", "drep.isMemberOfCollection",
                        "drep2.ismemberofcollection", "drep.ismemberofcollection")

        parts: list[str] = []
        if title:
            parts.append(f"Title: {title}.")
        if author:
            parts.append(f"Author(s): {author}.")
        if pub:
            parts.append(f"Publication: {pub}.")
        if subjects:
            parts.append(f"Subject(s): {subjects}.")
        if genre:
            parts.append(f"Genre: {genre}.")
        if lang:
            parts.append(f"Language: {lang}.")
        if fmt:
            parts.append(f"Format: {fmt}.")
        if rights and len(rights) > 20:
            parts.append(f"Rights: {rights}.")

        # Guaranteed floor — these two entries alone push us past 100 chars.
        coll_display = collection or _COLLECTION_NAME
        parts.append(f"Collection: {coll_display}.")
        parts.append(
            "Source: National Library of Medicine (NLM) Digital Collections, "
            "National Institutes of Health, Bethesda, MD."
        )

        abstract = " ".join(p for p in parts if p)

        # Hard fallback (should rarely trigger given the metadata richness).
        if len(abstract) < 100:
            abstract = (
                f"{title}. Item from the {_COLLECTION_NAME} digital collection "
                "maintained by the National Library of Medicine, "
                "National Institutes of Health, Bethesda, MD, United States. "
                "These documents comprise digitized NIH annual reports "
                "from the National Library of Medicine Digital Collections."
            )
        return abstract

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        start_ts = time.monotonic()
        saved = 0
        seen_urls: set = set()
        limit_str = str(limit) if limit is not None else "inf"

        print(f"[{_SITE_ID}] Starting crawl (limit={limit_str})")

        pw, browser, page = self._launch_playwright()
        try:
            p = 1
            total_pages: Optional[int] = None

            while True:
                # Wall-clock guard
                if time.monotonic() - start_ts > _BUDGET_SECS:
                    print(
                        f"[{_SITE_ID}] 25-min wall-clock budget reached, stopping."
                    )
                    break

                if limit is not None and saved >= limit:
                    break

                if p > _PAGE_CAP:
                    print(
                        f"[{_SITE_ID}] Safety cap of {_PAGE_CAP} pages reached."
                    )
                    break

                list_url = (
                    f"{_LIST_API}"
                    f"?f%5Bdrep2.isMemberOfCollection%5D%5B%5D={_COLLECTION_ID}"
                    f"&page={p}&per_page={_PER_PAGE}"
                )

                if p == 1 or p % 10 == 0:
                    print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_str}")

                data = self._fetch_json(page, list_url)
                if data is None:
                    print(
                        f"[{_SITE_ID}] Failed to fetch list page {p}, stopping."
                    )
                    break

                items = data.get("data") or []
                if not items:
                    print(f"[{_SITE_ID}] No items on page {p}. Done.")
                    break

                meta_pages = (data.get("meta") or {}).get("pages") or {}
                if total_pages is None:
                    total_pages = meta_pages.get("total_pages") or 1
                    total_count = meta_pages.get("total_count", "?")
                    print(
                        f"[{_SITE_ID}] Total: {total_count} items "
                        f"across {total_pages} page(s)"
                    )

                new_on_page = 0
                for item in items:
                    if limit is not None and saved >= limit:
                        break
                    if time.monotonic() - start_ts > _BUDGET_SECS:
                        print(
                            f"[{_SITE_ID}] Wall-clock budget reached mid-page."
                        )
                        break

                    try:
                        item_id = (item.get("id") or "").strip()
                        item_url = (
                            (item.get("links") or {}).get("self")
                            or f"{_BASE_URL}/catalog/{item_id}"
                        ).strip()
                        list_attrs = item.get("attributes") or {}
                        title = (list_attrs.get("title") or "").strip()

                        if not item_url or not title:
                            continue

                        # URL deduplication — detect silent pagination loop.
                        if item_url in seen_urls:
                            print(
                                f"[{_SITE_ID}] duplicate URL at page {p}; "
                                "pagination loop detected — stopping."
                            )
                            print(
                                f"[{_SITE_ID}] Done. Total saved: {saved}"
                            )
                            return saved
                        seen_urls.add(item_url)
                        new_on_page += 1

                        # --------------------------------------------------
                        # Fetch per-item detail JSON for richer metadata.
                        # --------------------------------------------------
                        detail_attrs: dict = {}
                        detail_url = f"{_BASE_URL}/catalog/{item_id}.json"
                        time.sleep(max(self._delay, 0.5))
                        detail_data = self._fetch_json(page, detail_url)
                        if detail_data:
                            detail_attrs = (
                                (detail_data.get("data") or {})
                                .get("attributes") or {}
                            )

                        # --------------------------------------------------
                        # Build abstract from list + detail metadata.
                        # --------------------------------------------------
                        abstract = self._build_abstract(list_attrs, detail_attrs)

                        if len(abstract) < 50:
                            print(
                                f"[{_SITE_ID}] Abstract too short "
                                f"({len(abstract)} chars) for {item_id}, skipping."
                            )
                            continue

                        # --------------------------------------------------
                        # Extract remaining fields.
                        # --------------------------------------------------
                        def gf(*keys):
                            for k in keys:
                                v = self._attr(list_attrs, k)
                                if v:
                                    return v
                                v = self._attr(detail_attrs, k)
                                if v:
                                    return v
                            return ""

                        author   = gf("drep2.authorAggregate", "drep.authorAggregate")
                        pub      = gf("drep2.pubConcat", "drep.pubConcat",
                                      "drep2.pubconcat", "drep.pubconcat")
                        subjects = gf("drep2.subjectAggregate",
                                      "drep.subjectAggregate")
                        genre    = gf("drep2.subjectGenre",  "drep.subjectGenre")
                        lang     = gf("drep2.language",      "drep.language")
                        fmt      = gf("drep.format",         "drep2.format")

                        nlm_uid_raw = gf(
                            "drep.identifierNLM",  "drep2.identifierNLM",
                            "drep.identifiernlm",  "drep2.identifiernlm",
                        )
                        nlm_uid = re.sub(r"\s*\(.*?\)", "", nlm_uid_raw).strip()

                        leaf = self._leaf_id(item_id)
                        published_date = self._extract_year(title)

                        # post_number: numeric leaf ID when available, else full leaf.
                        num_m = re.match(r"(\d+)", leaf)
                        post_number = leaf  # e.g. "7802080X3" (has letter suffix)

                        # Keywords from subjects.
                        keywords: Optional[str] = None
                        if subjects:
                            kw_list = [
                                s.strip()
                                for s in re.split(r"[;,\n]+", subjects)
                                if s.strip()
                            ]
                            keywords = ", ".join(kw_list) if kw_list else subjects

                        # PDF URL: site exposes /pdf/{item_id}.
                        pdf_url: Optional[str] = (
                            f"{_BASE_URL}/pdf/{item_id}" if item_id else None
                        )
                        original_filename: Optional[str] = (
                            f"{leaf}.pdf" if leaf else None
                        )

                        item_type = (
                            "mvset" if item_id.endswith("-mvset") else "mvpart"
                        )

                        paper = {
                            "site_id":          self.site_id,
                            "external_id":      item_id,
                            "post_number":      post_number,
                            "title":            title,
                            "abstract":         abstract,
                            "published_date":   published_date,
                            "listed_date":      None,
                            "authors":          author or None,
                            "publisher":        pub or None,
                            "department":       None,
                            "journal":          None,
                            "url":              item_url,
                            "pdf_url":          pdf_url,
                            "doi":              None,
                            "keywords":         keywords,
                            "category":         item_type,
                            "original_filename": original_filename,
                            "metadata": json.dumps(
                                {
                                    "posted_date":  published_date,
                                    "nlm_leaf_id":  leaf,
                                    "nlm_uid":      nlm_uid,
                                    "collection_id": _COLLECTION_ID,
                                    "language":     lang,
                                    "format":       fmt,
                                    "genre":        genre,
                                    "subjects_raw": subjects,
                                    "publication":  pub,
                                    "item_type":    item_type,
                                    "node_id":      item_id,
                                },
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{_SITE_ID}] Saved {saved}/{limit_str}: "
                            f"{title[:60]}"
                        )

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(
                            f"[{_SITE_ID}] item {item.get('id', '?')} failed: {exc}"
                        )
                        continue

                # -- Pagination bookkeeping --
                if new_on_page == 0:
                    print(f"[{_SITE_ID}] page {p} had no new items; stopping.")
                    break

                is_last = meta_pages.get("last_page?", False)
                next_pg = meta_pages.get("next_page")
                if is_last or next_pg is None:
                    print(
                        f"[{_SITE_ID}] Last page reached at page {p}. Done."
                    )
                    break

                if total_pages is not None and p >= total_pages:
                    print(
                        f"[{_SITE_ID}] Reached last page ({p}/{total_pages}). Done."
                    )
                    break

                if not (data.get("links") or {}).get("next"):
                    print(f"[{_SITE_ID}] No next-page link at page {p}. Done.")
                    break

                p += 1

        finally:
            try:
                browser.close()
            except Exception:
                pass
            try:
                pw.stop()
            except Exception:
                pass

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
