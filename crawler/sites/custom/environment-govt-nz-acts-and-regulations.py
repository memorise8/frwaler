# -*- coding: utf-8 -*-
"""Crawler for environment.govt.nz – acts-and-regulations/acts section.

Starting URL: https://environment.govt.nz/acts-and-regulations/acts/

The live site is behind Incapsula WAF which blocks non-browser TLS fingerprints
from this server's IP (incident edet=12, cinfo=04 = IP reputation block).

Strategy:
  1. Internet Archive CDX API → discover all unique content-page URLs under
     /acts-and-regulations/acts/, keeping the most-recent snapshot timestamp
     per URL.
  2. Wayback Machine snapshot fetch (if_ modifier = raw HTML, no toolbar
     redirect) per URL → parse SilverStripe HTML.
  3. Save via _save_paper().

CDX pagination: collapse=urlkey with a large limit to get all unique URLs in
one call. A second call scoped to 2024+ upgrades timestamps to recent snapshots.
Raw-record fallback is used if collapsed calls return nothing.
"""

import json
import re
import subprocess
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, '.')
from crawler.base_crawler import BaseCrawler

_SITE_ID = "environment-govt-nz-acts-and-regulations"
_CDX_API = "https://web.archive.org/cdx/search/cdx"
_WBM_BASE = "https://web.archive.org/web"
_BS_PARSERS = ("html5lib", "lxml", "html.parser")
# CDX URL pattern: only the /acts/ sub-section
_CDX_PATTERN = "environment.govt.nz/acts-and-regulations/acts/*"


def _make_soup(html):
    """Try parsers in order; return BeautifulSoup or None."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class EnvironmentGovtNzActsAndRegulationsCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: environment-govt-nz-acts-and-regulations"
    base_url = "https://environment.govt.nz"

    _DELAY = 1.0      # seconds between Wayback Machine page fetches
    _CDX_BATCH = 500  # raw CDX rows per API call

    # ------------------------------------------------------------------ helpers

    def _curl_get(self, url, retries=3):
        """Fetch URL via curl with exponential-backoff retry. Returns text or None."""
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-L",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(retries):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, timeout=35,
                )
                body = r.stdout.decode("utf-8", errors="replace") if r.stdout else ""
                if body.strip():
                    return body
            except Exception:
                pass
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)  # 1 s, 3 s, 9 s
                print(f"[{_SITE_ID}] curl retry {attempt + 1}/{retries} "
                      f"in {wait}s: {url[:80]}")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------ CDX discovery

    def _cdx_discover_all(self):
        """Discover unique content-page URLs under /acts-and-regulations/acts/ via CDX API.

        Strategy:
        - Phase 1: collapse=urlkey, limit=3000, no mimetype filter (mimetype
          filter is unreliable on the CDX API for this domain).
        - Phase 2: upgrade timestamps using a 2024+ scoped query.
        - Fallback: raw (non-collapsed) records if Phase 1 returns nothing.

        Returns list of {'url': str, 'timestamp': str} sorted alphabetically.
        """
        _LIMIT = 3000

        seen = {}  # url → best timestamp

        # --- Phase 1: all-time unique URLs ---
        cdx_url = (
            f"{_CDX_API}?url={_CDX_PATTERN}"
            f"&output=json&fl=timestamp,original&collapse=urlkey"
            f"&filter=statuscode:200"
            f"&limit={_LIMIT}"
        )
        raw = self._curl_get(cdx_url)
        if raw:
            try:
                data = json.loads(raw)
                for row in data[1:]:
                    ts, url = row[0], row[1]
                    if "?" not in url and "#" not in url:
                        seen[url] = ts
            except (json.JSONDecodeError, ValueError):
                pass

        if not seen:
            # Fallback: raw records without collapse (CDX collapse may be empty)
            print(f"[{_SITE_ID}] CDX Phase 1 empty; using raw-record fallback.")
            raw2 = self._curl_get(
                f"{_CDX_API}?url={_CDX_PATTERN}"
                f"&output=json&fl=timestamp,original"
                f"&filter=statuscode:200"
                f"&limit=2000"
            )
            if raw2:
                try:
                    data2 = json.loads(raw2)
                    for row in data2[1:]:
                        ts, url = row[0], row[1]
                        if "?" not in url and "#" not in url:
                            if url not in seen or ts > seen[url]:
                                seen[url] = ts
                except (json.JSONDecodeError, ValueError):
                    pass

        # --- Phase 2: upgrade timestamps with recent (2024+) snapshots ---
        time.sleep(0.3)
        cdx_recent = (
            f"{_CDX_API}?url={_CDX_PATTERN}"
            f"&output=json&fl=timestamp,original&collapse=urlkey"
            f"&filter=statuscode:200"
            f"&from=20240101000000&limit={_LIMIT}"
        )
        raw_r = self._curl_get(cdx_recent)
        if raw_r:
            try:
                data_r = json.loads(raw_r)
                for row in data_r[1:]:
                    ts, url = row[0], row[1]
                    if "?" not in url and "#" not in url:
                        if url not in seen or ts > seen[url]:
                            seen[url] = ts
            except (json.JSONDecodeError, ValueError):
                pass

        # Filter to content pages (>= 3 path segments) and sort alphabetically
        result = []
        for url, timestamp in seen.items():
            if self._is_content_page(url):
                result.append({"url": url, "timestamp": timestamp})

        result.sort(key=lambda e: e["url"])
        return result

    @staticmethod
    def _is_content_page(url):
        """True if URL is a detail page, not a top-level category index."""
        if "?" in url or "#" in url:
            return False
        parsed = urlparse(url)
        segments = [s for s in parsed.path.rstrip("/").split("/") if s]
        # Need at least: acts-and-regulations / <section> / <page-slug>
        return len(segments) >= 3

    # ------------------------------------------------------------------ fetch / parse

    def _fetch_wayback(self, timestamp, url):
        """Fetch URL from Wayback Machine snapshot. if_ = raw HTML, no toolbar."""
        wb_url = f"{_WBM_BASE}/{timestamp}if_/{url}"
        return self._curl_get(wb_url)

    def _parse_page(self, html, original_url):
        """Parse SilverStripe HTML, return field dict or None."""
        soup = _make_soup(html)
        if not soup:
            return None

        # --- Title ---
        title = ""
        h1 = soup.find("h1")
        if h1:
            title = h1.get_text(strip=True)
        if not title:
            og = soup.find("meta", property="og:title")
            if og:
                title = (og.get("content") or "").strip()
        if not title:
            t = soup.find("title")
            if t:
                title = re.sub(r"\s*\|.*$", "", t.get_text(strip=True)).strip()

        # --- Published date ---
        published_date = ""
        pm = soup.find("meta", property="article:published_time")
        if pm:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", pm.get("content", ""))
            if m:
                published_date = m.group(1)
        if not published_date:
            mm = soup.find("meta", property="article:modified_time")
            if mm:
                m2 = re.match(r"(\d{4}-\d{2}-\d{2})", mm.get("content", ""))
                if m2:
                    published_date = m2.group(1)

        # --- Short description ---
        desc_meta = soup.find("meta", attrs={"name": "description"})
        description = (desc_meta.get("content") or "").strip() if desc_meta else ""

        # --- Abstract: full main-content text ---
        abstract = ""
        main_el = soup.find("main")
        if main_el:
            for el in main_el.find_all(["script", "style", "nav", "noscript"]):
                el.decompose()
            text = main_el.get_text(separator=" ", strip=True)
            abstract = re.sub(r"\s+", " ", text).strip()
        if not abstract:
            abstract = description

        # --- PDF URL ---
        pdf_url = None
        original_filename = None
        pdf_link = soup.find("a", href=re.compile(r"\.pdf", re.IGNORECASE))
        if pdf_link:
            href = pdf_link.get("href", "")
            # Strip Wayback Machine wrapper
            href = re.sub(r"^https?://web\.archive\.org/web/\d+(?:if_)?/", "", href)
            href = re.sub(r"^/web/\d+(?:if_)?/", "", href)
            if href.startswith("/"):
                href = "https://environment.govt.nz" + href
            elif not href.startswith("http"):
                href = "https://environment.govt.nz/" + href
            pdf_url = href
            tail = href.split("/")[-1].split("?")[0]
            original_filename = tail if tail else None

        # --- Identifiers from URL path ---
        parsed = urlparse(original_url)
        segments = [s for s in parsed.path.rstrip("/").split("/") if s]
        # Full path used as external_id — globally unique
        external_id = parsed.path.strip("/")
        # Last slug as post_number (no numeric IDs on this CMS)
        post_number = segments[-1] if segments else None
        # Category from second segment
        category = segments[1] if len(segments) >= 2 else "acts-and-regulations"

        return {
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "url": original_url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "New Zealand Ministry for the Environment",
            "category": category,
            "keywords": None,
            "metadata": json.dumps({
                "description": description,
                "path": parsed.path,
                "section": category,
                "posted_date": published_date,
            }, ensure_ascii=False),
        }

    # ------------------------------------------------------------------ crawl

    def crawl(self, limit=None):
        """Crawl acts-and-regulations pages from Wayback Machine snapshots.

        Parameters
        ----------
        limit : int or None
            Max records to save. None = unlimited.
        """
        start_time = time.time()
        MAX_WALL_SEC = 25 * 60   # 25-minute wall-clock budget
        SAFETY_CAP = 200         # max CDX discovery pages (already handled per-batch)

        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")

        # --- Discover all candidate URLs up-front ---
        print(f"[{_SITE_ID}] Discovering URLs via CDX API …")
        all_entries = self._cdx_discover_all()
        print(f"[{_SITE_ID}] Discovered {len(all_entries)} candidate content pages.")

        # --- Process URLs one by one ---
        for idx, entry in enumerate(all_entries):
            if saved >= limit_or_inf:
                break

            # Wall-clock budget
            if time.time() - start_time > MAX_WALL_SEC:
                print(f"[{_SITE_ID}] Wall-clock budget reached after {idx} items, stopping.")
                break

            url = entry["url"]
            timestamp = entry["timestamp"]

            # URL deduplication across pages
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Progress log every 10 items
            if idx > 0 and idx % 10 == 0:
                p = idx // 10
                print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_or_inf}")

            try:
                time.sleep(self._DELAY)

                html = self._fetch_wayback(timestamp, url)
                if not html:
                    print(f"[{_SITE_ID}] fetch failed: {url}")
                    continue

                data = self._parse_page(html, url)
                if not data:
                    print(f"[{_SITE_ID}] parse failed: {url}")
                    continue

                abstract = data.get("abstract", "")
                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] abstract <50 chars ({len(abstract)}) "
                          f"for {url}, skipping.")
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": data["external_id"],
                    "post_number": data["post_number"],
                    "title": data["title"] or url,
                    "abstract": data["abstract"],
                    "published_date": data["published_date"],
                    "listed_date": data["listed_date"],
                    "url": data["url"],
                    "pdf_url": data["pdf_url"],
                    "original_filename": data["original_filename"],
                    "publisher": data["publisher"],
                    "category": data["category"],
                    "keywords": data["keywords"],
                    "metadata": data["metadata"],
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{_SITE_ID}] Saved {saved}/{limit_or_inf}: "
                      f"{data['title'][:60]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {url} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
