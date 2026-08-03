# -*- coding: utf-8 -*-
"""Stats NZ Publications crawler.

Target: https://www.stats.govt.nz/publications/?categoryFiltersID=385&filters=Reports

The site uses SilverStripe CMS and embeds all page data as JSON in:
    <div id="pageViewData" data-value="{...}">

Listing pages include a ``PaginatedBlockPages`` list; individual publication
pages include ``MetaDescription``, ``FeaturedText``, ``PageDate``, etc.

Access strategy:
  1. Try direct HTTP fetch (works from NZ IPs).
  2. If Incapsula blocks (non-NZ IPs), fall back to Common Crawl CDX + WARC.
"""

from __future__ import annotations

import gzip
import html as htmllib
import io
import json
import os
import re
import subprocess
import time

try:
    from bs4 import BeautifulSoup as _BS4
    _BS4_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS4 = None
    _BS4_PARSERS = []

from crawler.base_crawler import BaseCrawler

# ── Constants ─────────────────────────────────────────────────────────────────
_BASE_URL = "https://www.stats.govt.nz"
_LISTING_URL = _BASE_URL + "/publications/?categoryFiltersID=385&filters=Reports"
_CC_INDEX = "CC-MAIN-2024-51"
_CC_CDX_API = f"https://index.commoncrawl.org/{_CC_INDEX}-index"
_CC_WARC_BASE = "https://data.commoncrawl.org/"
_PAGE_SIZE = 12   # items per listing page (observed from live site)
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap


# ── HTML / JSON helpers ───────────────────────────────────────────────────────

def _strip_html(s: str) -> str:
    if not s:
        return ""
    text = re.sub(r"<[^>]+>", " ", str(s))
    text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_pv_data(html_str: str) -> dict | None:
    """Return the pageViewData JSON dict embedded in a stats.govt.nz page."""
    # Fast path: regex (avoids full BS4 parse when possible)
    for pat in [
        r'id="pageViewData"\s+data-value="([^"]+)"',
        r"id='pageViewData'\s+data-value='([^']+)'",
        r'data-value="([^"]+)"\s+id="pageViewData"',
    ]:
        m = re.search(pat, html_str)
        if m:
            try:
                return json.loads(htmllib.unescape(m.group(1)))
            except json.JSONDecodeError:
                pass

    # BS4 fallback for malformed / unusual HTML
    if _BS4:
        for parser in _BS4_PARSERS:
            try:
                soup = _BS4(html_str, parser)
                el = soup.find(id="pageViewData")
                if el and el.get("data-value"):
                    return json.loads(htmllib.unescape(el["data-value"]))
            except Exception:
                continue
    return None


def _build_abstract(pv: dict) -> str:
    """Combine MetaDescription + FeaturedText + first PageBlocks body."""
    parts: list[str] = []
    seen: set[str] = set()

    def _add(text: str) -> None:
        t = text.strip()
        if t and t not in seen:
            seen.add(t)
            parts.append(t)

    _add((pv.get("MetaDescription") or ""))
    _add(_strip_html(pv.get("FeaturedText") or ""))

    # Pull first substantial PageBlocks content for extra depth
    for blk in (pv.get("PageBlocks") or []):
        if not isinstance(blk, dict):
            continue
        body = _strip_html(
            blk.get("Content") or blk.get("Body") or blk.get("Text") or ""
        )
        if body and len(body) > 80:
            _add(body[:1000])
            break

    return "\n\n".join(parts)


def _parse_date(raw: str) -> str:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", raw or "")
    return m.group(1) if m else ""


# ── Crawler ───────────────────────────────────────────────────────────────────

class StatsGovtNzPublicationsCrawler(BaseCrawler):
    site_id = "stats-govt-nz-publications"
    site_name = "Custom: stats-govt-nz-publications"
    base_url = _BASE_URL

    # ── Low-level network ─────────────────────────────────────────────────────

    def _curl_get(self, url: str, retries: int = 3, timeout: int = 30) -> str | None:
        """GET via curl with retries; returns response text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        for attempt in range(retries):
            try:
                res = subprocess.run(
                    cmd, capture_output=True, text=True,
                    encoding="utf-8", errors="replace",
                    timeout=timeout + 10,
                )
                if res.stdout.strip():
                    return res.stdout
            except Exception as exc:
                if attempt < retries - 1:
                    wait = [1, 3, 9][attempt]
                    print(f"[{self.site_id}] curl error (attempt {attempt+1}/{retries}): {exc}")
                    time.sleep(wait)
        return None

    @staticmethod
    def _is_blocked(html: str | None) -> bool:
        if not html:
            return True
        lc = html.lower()
        return "incapsula" in lc or "_incapsula_resource" in lc

    # ── Common Crawl helpers ──────────────────────────────────────────────────

    def _cc_cdx_query(self, url_pattern: str, limit: int = 500) -> list[dict]:
        api = (
            f"{_CC_CDX_API}?url={url_pattern}"
            f"&output=json&filter=statuscode:200&limit={limit}"
        )
        cmd = ["curl", "-skL", "--max-time", "30", api]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
                records = []
                for line in res.stdout.strip().splitlines():
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
                return records
            except Exception as exc:
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
        return []

    def _cc_fetch_warc(self, record: dict) -> str | None:
        """Fetch one WARC record by byte range; return HTML body or None."""
        offset = int(record.get("offset", 0))
        length = int(record.get("length", 0))
        filename = record.get("filename", "")
        if not filename or not length:
            return None
        end = offset + length - 1
        warc_url = _CC_WARC_BASE + filename
        cmd = ["curl", "-skL", "--max-time", "60",
               "-H", f"Range: bytes={offset}-{end}", warc_url]
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=70)
                if not res.stdout:
                    raise ValueError("empty response")
                with gzip.open(io.BytesIO(res.stdout)) as f:
                    raw = f.read().decode("utf-8", errors="replace")
                # Strip WARC headers (ends at first blank line)
                for sep in ("\r\n\r\n", "\n\n"):
                    i = raw.find(sep)
                    if i >= 0:
                        raw = raw[i + len(sep):]
                        break
                # Strip HTTP response headers
                for sep in ("\r\n\r\n", "\n\n"):
                    i = raw.find(sep)
                    if i >= 0:
                        raw = raw[i + len(sep):]
                        break
                return raw
            except Exception as exc:
                if attempt < 2:
                    time.sleep((attempt + 1) * 3)
        return None

    # ── Paper building ────────────────────────────────────────────────────────

    def _make_paper(self, pv: dict, url: str) -> dict | None:
        """Build a paper_dict from a pageViewData JSON object. Returns None if unusable."""
        title = (pv.get("Title") or "").strip()
        if not title:
            return None

        abstract = _build_abstract(pv)
        if len(abstract) < 100:
            print(f"[{self.site_id}] skip (abstract {len(abstract)} chars): {title[:60]}")
            return None

        page_link = pv.get("PageLink") or ""
        if page_link and not page_link.startswith("http"):
            page_link = f"{self.base_url}{page_link}"

        terms_topics = [
            t.get("Title") or ""
            for t in (pv.get("TermsTopics") or [])
            if isinstance(t, dict)
        ]
        category = ", ".join(filter(None, terms_topics))

        terms_releases = [
            t.get("Title") or ""
            for t in (pv.get("TermsReleases") or [])
            if isinstance(t, dict) and t.get("Title")
        ]

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(pv.get("ID") or ""),
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": category,
            "keywords": json.dumps(terms_releases, ensure_ascii=False),
            "published_date": _parse_date(pv.get("PageDate") or ""),
            "url": page_link or url or "",
            "pdf_url": "",
            "doi": "",
            "department": "Statistics New Zealand",
            "metadata": json.dumps({
                "source": "stats.govt.nz",
                "className": pv.get("ClassName") or "",
                "categoryFiltersID": "385",
                "filters": "Reports",
            }, ensure_ascii=False),
        }

    def _make_paper_from_listing_item(self, item: dict, url: str) -> dict | None:
        """Build paper_dict from a PaginatedBlockPages item (listing page data)."""
        title = (item.get("Title") or "").strip()
        if not title:
            return None

        meta = (item.get("MetaDescription") or "").strip()
        ft = _strip_html(item.get("FeaturedText") or "")
        parts: list[str] = []
        seen: set[str] = set()
        for t in [meta, ft]:
            if t and t not in seen:
                seen.add(t)
                parts.append(t)
        abstract = "\n\n".join(parts)

        if len(abstract) < 100:
            print(f"[{self.site_id}] skip listing item (abstract {len(abstract)} chars): {title[:60]}")
            return None

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": str(item.get("ID") or ""),
            "title": title,
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": abstract,
            "category": "",
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": _parse_date(item.get("PageDate") or ""),
            "url": url or "",
            "pdf_url": "",
            "doi": "",
            "department": "Statistics New Zealand",
            "metadata": json.dumps({"source": "stats.govt.nz"}, ensure_ascii=False),
        }

    # ── Main crawl entry point ────────────────────────────────────────────────

    def crawl(self, limit: int | None = None) -> int:
        t0 = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes
        seen_urls: set[str] = set()

        print(f"[{self.site_id}] Checking direct access …")
        probe = self._curl_get(_LISTING_URL, retries=2, timeout=20)
        if probe and not self._is_blocked(probe):
            print(f"[{self.site_id}] Direct access OK — crawling listing pages.")
            saved = self._crawl_direct(limit, seen_urls, t0, MAX_WALL, probe)
        else:
            print(f"[{self.site_id}] Direct access blocked — using Common Crawl fallback.")
            saved = self._crawl_via_cc(limit, seen_urls, t0, MAX_WALL)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ── Direct-access path ────────────────────────────────────────────────────

    def _crawl_direct(
        self,
        limit: int | None,
        seen_urls: set[str],
        t0: float,
        max_wall: float,
        first_page_html: str,
    ) -> int:
        saved = 0
        page = 0
        total_pages: int | None = None
        limit_s = str(limit) if limit is not None else "∞"

        while True:
            if time.time() - t0 > max_wall:
                print(f"[{self.site_id}] Wall-clock budget hit at page {page}. Stopping.")
                break
            if limit is not None and saved >= limit:
                break
            if page >= _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap ({_MAX_PAGES} pages) reached. Stopping.")
                break
            if total_pages is not None and page >= total_pages:
                print(f"[{self.site_id}] All {total_pages} pages processed. Done.")
                break

            # Fetch the page HTML (reuse probe HTML for page 0)
            if page == 0:
                html = first_page_html
            else:
                start = page * _PAGE_SIZE
                url = f"{_LISTING_URL}&start={start}"
                html = None
                for attempt in range(3):
                    time.sleep(self._delay)
                    html = self._curl_get(url)
                    if html and not self._is_blocked(html):
                        break
                    wait = [1, 3, 9][min(attempt, 2)]
                    print(f"[{self.site_id}] page {page} fetch attempt {attempt+1}/3 failed, wait {wait}s")
                    time.sleep(wait)
                    html = None
                if not html:
                    print(f"[{self.site_id}] Cannot fetch page {page}. Stopping.")
                    break

            pv = _parse_pv_data(html)
            if not pv:
                print(f"[{self.site_id}] No pageViewData on page {page}. Stopping.")
                break

            items = pv.get("PaginatedBlockPages") or []
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Done.")
                break

            if page == 0:
                total_pages = int(pv.get("totalPages") or 0) or None
                print(f"[{self.site_id}] totalPages={total_pages}, items on page 0: {len(items)}")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break
                page_link = item.get("PageLink") or ""
                if page_link and not page_link.startswith("http"):
                    page_link = f"{self.base_url}{page_link}"
                if page_link in seen_urls:
                    continue
                seen_urls.add(page_link)
                new_on_page += 1

                try:
                    paper = self._make_paper_from_listing_item(item, page_link)
                    if paper is None and page_link:
                        # Abstract too short from listing — fetch detail page
                        time.sleep(self._delay)
                        detail_html = self._curl_get(page_link)
                        if detail_html and not self._is_blocked(detail_html):
                            detail_pv = _parse_pv_data(detail_html)
                            if detail_pv:
                                paper = self._make_paper(detail_pv, page_link)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_s}: {paper['title'][:70]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed ({page_link}): {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen. Done.")
                break

            if page % 10 == 0 and page > 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_s}")

            page += 1

        return saved

    # ── Common Crawl fallback ─────────────────────────────────────────────────

    def _crawl_via_cc(
        self,
        limit: int | None,
        seen_urls: set[str],
        t0: float,
        max_wall: float,
    ) -> int:
        saved = 0
        limit_s = str(limit) if limit is not None else "∞"
        cdx_limit = min(1000, max(200, (limit or 50) * 10))

        # Collect CC records from multiple URL patterns
        patterns = [
            "www.stats.govt.nz/information-releases/*",
            "www.stats.govt.nz/reports/*",
            "www.stats.govt.nz/publications/*",
        ]
        all_records: list[dict] = []
        for pat in patterns:
            recs = self._cc_cdx_query(pat, limit=cdx_limit)
            print(f"[{self.site_id}] CC CDX '{pat}': {len(recs)} records")
            all_records.extend(recs)
            if limit is not None and len(all_records) >= limit * 5:
                break

        # Deduplicate by URL (keep first occurrence)
        seen_cc: dict[str, dict] = {}
        for r in all_records:
            u = r.get("url", "")
            if u not in seen_cc:
                seen_cc[u] = r
        unique = list(seen_cc.values())
        print(f"[{self.site_id}] {len(unique)} unique CC records to process")

        for i, record in enumerate(unique):
            if time.time() - t0 > max_wall:
                print(f"[{self.site_id}] Wall-clock budget hit. Stopping.")
                break
            if limit is not None and saved >= limit:
                break

            url = record.get("url", "")
            if url in seen_urls:
                continue
            seen_urls.add(url)

            try:
                html = self._cc_fetch_warc(record)
                if not html:
                    continue

                pv = _parse_pv_data(html)
                if not pv:
                    continue

                # Listing pages have PaginatedBlockPages; process each item
                paged_items = pv.get("PaginatedBlockPages")
                if paged_items:
                    for item in paged_items:
                        if limit is not None and saved >= limit:
                            break
                        page_link = item.get("PageLink") or ""
                        if page_link and not page_link.startswith("http"):
                            page_link = f"{self.base_url}{page_link}"
                        if page_link in seen_urls:
                            continue
                        seen_urls.add(page_link)
                        paper = self._make_paper_from_listing_item(item, page_link)
                        if paper is None:
                            continue
                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}/{limit_s}: {paper['title'][:70]}")
                else:
                    # Individual publication page
                    paper = self._make_paper(pv, url)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_s}: {paper['title'][:70]}")

                if i % 10 == 0 and i > 0:
                    print(f"[{self.site_id}] page {i // 10}: saved {saved}/{limit_s}")

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] record {url} failed: {exc}")
                continue

        return saved
