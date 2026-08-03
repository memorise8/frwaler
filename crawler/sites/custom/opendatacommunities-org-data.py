# -*- coding: utf-8 -*-
"""Crawler for opendatacommunities.org/data (→ open-data.communities.gov.uk).

The target site is a static SvelteKit SPA.  All page content is embedded
as HTML string literals inside JS "node" files served from the same CDN.
Strategy:
  1. Fetch start URL (follows 308 redirect to open-data.communities.gov.uk).
  2. Parse HTML shell → discover app bundle URL.
  3. Parse app bundle → extract __vite__mapDeps → node file list.
  4. Fetch each node file; detect dataset detail pages by class presence.
  5. Extract metadata and save via _save_paper.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape

try:
    from bs4 import BeautifulSoup
    _BS_PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    BeautifulSoup = None
    _BS_PARSERS: list[str] = []

from crawler.base_crawler import BaseCrawler

_SITE_ID = "opendatacommunities-org-data"
_NEW_BASE = "https://open-data.communities.gov.uk"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))  # safety cap on node files to scan
_ABSTRACT_MIN_CHARS = 50

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
}


def _make_soup(text: str):
    """Parse HTML with best available parser, fallback chain."""
    if not BeautifulSoup:
        return None
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(text, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str | None) -> str | None:
    """Convert 'September 2015', '2015-09-30', 'October 2025' → YYYY-MM-DD."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return raw[:10]
    m = re.search(r"(\w+)\s+(\d{4})", raw)
    if m:
        mon = m.group(1).lower()
        if mon in _MONTHS:
            return f"{m.group(2)}-{_MONTHS[mon]}-01"
    m = re.search(r"(\d{4})", raw)
    if m:
        return f"{m.group(1)}-01-01"
    return None


class OpenDataCommunitiesDataCrawler(BaseCrawler):
    """Crawler for opendatacommunities.org/data (MHCLG Open Data portal)."""

    site_id = "opendatacommunities-org-data"
    site_name = "Custom: opendatacommunities-org-data"
    base_url = "https://opendatacommunities.org"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url: str, *, retries: int = 3, timeout: int = 30) -> str:
        """Fetch URL via curl with retry + exponential backoff."""
        waits = [1, 3, 9]
        last_err = "unknown"
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-skL",
                        "--connect-timeout", "15",
                        "--max-time", str(timeout),
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        "-H", "Accept: text/html,application/json,*/*;q=0.8",
                        url,
                    ],
                    capture_output=True,
                    timeout=timeout + 15,
                )
                raw = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and raw.strip():
                    return raw
                last_err = f"exit={result.returncode}"
            except Exception as exc:
                last_err = str(exc)

            if attempt < retries - 1:
                wait = waits[attempt]
                print(
                    f"[{_SITE_ID}] curl attempt {attempt+1}/{retries} failed "
                    f"for {url}: {last_err}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{_SITE_ID}] All {retries} curl attempts failed for {url}: {last_err}")
        return ""

    # ------------------------------------------------------------------
    # App bundle / node-file discovery
    # ------------------------------------------------------------------

    def _get_app_bundle_url(self, html: str) -> str | None:
        """Parse app bundle URL from SvelteKit page HTML."""
        m = re.search(r"/app/immutable/entry/app\.[^\"'<>\s]+\.js", html)
        if m:
            return f"{_NEW_BASE}{m.group(0)}"
        return None

    def _get_node_filenames(self, bundle_js: str) -> list[str]:
        """Parse __vite__mapDeps to get all node/*.js filenames."""
        m = re.search(r"m\.f\s*\|\|\s*\(m\.f\s*=\s*(\[[^\]]{50,}\])\)", bundle_js)
        if not m:
            return []
        try:
            all_deps: list[str] = json.loads(m.group(1))
        except (ValueError, TypeError):
            return []
        return [d for d in all_deps if isinstance(d, str) and d.startswith("../nodes/")]

    # ------------------------------------------------------------------
    # HTML extraction from JS node file
    # ------------------------------------------------------------------

    def _extract_html_from_node(self, js_content: str) -> str:
        """Extract embedded page HTML from a SvelteKit node JS file.

        SvelteKit embeds page templates as single-quoted JS strings ending
        with ',1)' or ', 1)'.  HTML attributes use double quotes so no
        single-quote escaping occurs inside the template.
        """
        idx = js_content.find("'<!>")
        if idx == -1:
            return ""
        for end_marker in ("',1)", "', 1)"):
            end_idx = js_content.find(end_marker, idx)
            if end_idx != -1:
                return js_content[idx + 1 : end_idx]
        return ""

    # ------------------------------------------------------------------
    # Dataset metadata extraction
    # ------------------------------------------------------------------

    def _parse_dataset(self, node_js: str, node_url: str) -> dict | None:
        """Parse dataset metadata from a SvelteKit node JS file.

        Returns a paper dict suitable for _save_paper, or None if this
        node is not a dataset detail page.
        """
        # Fast pre-filter: dataset detail pages always contain both classes
        if "page-title" not in node_js or "dataset-description" not in node_js:
            return None

        raw_html = self._extract_html_from_node(node_js)
        if not raw_html:
            return None

        try:
            soup = _make_soup(raw_html)
        except Exception as exc:
            print(f"[{_SITE_ID}] BeautifulSoup failed for {node_url}: {exc}")
            return None

        if not soup:
            return None

        # Title
        title_el = soup.find(class_="page-title")
        if not title_el:
            return None
        title = unescape(re.sub(r"\s+", " ", title_el.get_text(" ", strip=True))).strip()
        if not title:
            return None

        # Abstract: primary source is dataset-description paragraph
        desc_el = soup.find(class_="dataset-description")
        if not desc_el:
            return None
        abstract = unescape(
            re.sub(r"\s+", " ", desc_el.get_text(" ", strip=True))
        ).strip()

        # Supplement abstract if too short using domain list items
        if len(abstract) < 100:
            about = soup.find(class_="about-section")
            if about:
                domain_items = [
                    li.get_text(" ", strip=True) for li in about.find_all("li")
                ]
                if domain_items:
                    abstract += " Domains: " + "; ".join(domain_items[:4])

        # Slug from CSVW link
        slug = None
        csvw_link = soup.find("a", href=re.compile(r"/datasets/.+/csvw"))
        if csvw_link:
            m = re.search(r"/datasets/([^/\"'\s]+)/csvw", csvw_link.get("href", ""))
            if m:
                slug = m.group(1)
        if not slug:
            for a in soup.find_all("a", href=True):
                m = re.search(r"/datasets/(indices-of-[^/\"'\s]+)", a["href"])
                if m:
                    slug = m.group(1)
                    break
        if not slug:
            return None

        detail_url = f"{_NEW_BASE}/datasets/{slug}"

        # Published date
        pub_raw = None
        for h3 in soup.find_all(["h3", "h2"]):
            if "Published" in h3.get_text():
                p_el = h3.find_next_sibling("p")
                if not p_el and h3.parent:
                    p_el = h3.parent.find("p")
                if p_el:
                    pub_raw = p_el.get_text(strip=True)
                    break
        published_date = _parse_date(pub_raw)

        # Coverage and frequency
        coverage = None
        frequency = None
        for h3 in soup.find_all("h3"):
            txt = h3.get_text(strip=True)
            sib_p = h3.find_next_sibling("p")
            if not sib_p and h3.parent:
                sib_p = h3.parent.find("p")
            val = sib_p.get_text(strip=True) if sib_p else None
            if not val:
                continue
            if "Geographic" in txt:
                coverage = val
            elif "Frequency" in txt:
                frequency = val

        # Statistical release link
        release_url = None
        for a in soup.find_all("a", href=re.compile(r"gov\.uk/government/statistics")):
            release_url = a.get("href", "")
            break

        publisher = "Ministry of Housing, Communities & Local Government"
        csvw_url = f"{_NEW_BASE}/datasets/{slug}/csvw"

        keywords = (
            "indices of deprivation, IMD, England, LSOA, "
            "income deprivation, employment deprivation, education deprivation, "
            "health deprivation, crime deprivation, housing deprivation, "
            "living environment deprivation"
        )

        meta = {
            "slug": slug,
            "frequency": frequency,
            "coverage": coverage,
            "statistical_release_url": release_url,
            "csvw_url": csvw_url,
            "license": "Open Government Licence v3.0",
        }

        return {
            "site_id": _SITE_ID,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": published_date,
            "url": detail_url,
            "pdf_url": csvw_url,
            "publisher": publisher,
            "keywords": keywords,
            "category": "Deprivation Statistics",
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:  # noqa: D102
        limit_or_inf = limit if limit is not None else float("inf")
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        # 1. Fetch start page (follows 308 redirect to _NEW_BASE)
        print(f"[{_SITE_ID}] Starting crawl from {self.base_url}/data")
        html = self._curl(f"{self.base_url}/data")
        if not html:
            print(f"[{_SITE_ID}] Failed to fetch start URL")
            return 0

        # 2. Locate app bundle
        app_bundle_url = self._get_app_bundle_url(html)
        if not app_bundle_url:
            print(f"[{_SITE_ID}] Could not find app bundle URL in HTML")
            return 0
        print(f"[{_SITE_ID}] App bundle: {app_bundle_url}")

        # 3. Discover node files from __vite__mapDeps
        app_bundle = self._curl(app_bundle_url)
        if not app_bundle:
            print(f"[{_SITE_ID}] Failed to fetch app bundle")
            return 0

        node_paths = self._get_node_filenames(app_bundle)
        if not node_paths:
            print(f"[{_SITE_ID}] No node files found in app bundle")
            return 0
        print(f"[{_SITE_ID}] Found {len(node_paths)} node files")

        # 4. Scan each node file for dataset pages
        p = 0
        for node_path in node_paths:
            if saved >= limit_or_inf:
                break
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{_SITE_ID}] 25-minute wall-clock budget reached, stopping")
                break
            if p >= _MAX_PAGES:
                print(f"[{_SITE_ID}] Safety cap of {_MAX_PAGES} pages reached")
                break

            node_name = node_path.replace("../nodes/", "")
            node_url = f"{_NEW_BASE}/app/immutable/nodes/{node_name}"
            p += 1

            if p % 10 == 0:
                print(f"[{_SITE_ID}] page {p}: saved {saved}/{limit_or_inf}")

            try:
                node_js = self._curl(node_url)
                if not node_js:
                    continue

                dataset = self._parse_dataset(node_js, node_url)
                if not dataset:
                    continue

                title = dataset.get("title", "")
                abstract = dataset.get("abstract", "")
                detail_url = dataset.get("url", "")

                if not title:
                    continue

                if len(abstract) < _ABSTRACT_MIN_CHARS:
                    print(
                        f"[{_SITE_ID}] Skipping '{title}' — "
                        f"abstract too short ({len(abstract)} chars)"
                    )
                    continue

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                self._save_paper(dataset)
                saved += 1
                print(
                    f"[{_SITE_ID}] Saved ({saved}): {title} "
                    f"| abstract={len(abstract)} chars"
                )

                time.sleep(self._delay)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] Error processing {node_url}: {exc}")
                continue

        print(f"[{_SITE_ID}] Done: saved {saved} records")
        return saved
