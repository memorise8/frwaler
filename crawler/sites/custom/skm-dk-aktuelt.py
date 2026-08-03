# -*- coding: utf-8 -*-
"""SKM.dk press releases crawler — skm-dk-aktuelt.

Strategy:
  1. Parse sitemap.xml to discover all press-release URLs.
  2. Detect the current Next.js buildId from the listing page.
  3. For each detail URL, fetch /_next/data/{buildId}/...json to get clean JSON
     (avoids parsing full HTML; falls back to HTML + __NEXT_DATA__ on 404).
  4. Extract title, teaser, full body text, date, tags, etc. and persist.
"""

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone

from crawler.base_crawler import BaseCrawler

_BASE = "https://skm.dk"
_SITEMAP_URL = "https://skm.dk/sitemap.xml"
_LISTING_PATH = "/aktuelt/presse-nyheder/pressemeddelelser"
_LISTING_URL = _BASE + _LISTING_PATH

# Sitemap entry prefix for press releases
_SITEMAP_PREFIX = _BASE + _LISTING_PATH + "/"

_RATE_SLEEP = 1.0      # seconds between detail fetches
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))       # safety cap (not real pages here, kept for spec compliance)
_WALL_MINUTES = 25     # budget per crawl


# ---------------------------------------------------------------------------
# curl helper (TLS workaround)
# ---------------------------------------------------------------------------

def _curl_get(url: str, retries: int = 3) -> str | None:
    """GET via curl with TLS-1.3 fallback; returns body text or None."""
    cmd = [
        "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
        "-H", "Accept: text/html,application/json,application/xml,*/*;q=0.8",
        "-H", "Accept-Language: da-DK,da;q=0.9,en;q=0.7",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(
                cmd, capture_output=True, timeout=40
            )
            body = result.stdout.decode("utf-8", errors="replace")
            if body.strip():
                return body
        except Exception as exc:
            print(f"[skm-dk-aktuelt] curl error (attempt {attempt+1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = (2 ** attempt)          # 1s, 2s
            time.sleep(wait)
    return None


# ---------------------------------------------------------------------------
# HTML / text helpers
# ---------------------------------------------------------------------------

def _strip_tags(html: str) -> str:
    """Remove HTML tags and normalise whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&[a-zA-Z]+;", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_date(raw: str) -> str:
    """Return ISO YYYY-MM-DD from an ISO-8601 string, or '' on failure."""
    if not raw:
        return ""
    try:
        return raw[:10]          # '2023-09-12T00:00:00Z' → '2023-09-12'
    except Exception:
        return ""


def _bs_parse(html: str):
    """Try html5lib → lxml → html.parser; return BeautifulSoup or None."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _extract_next_data(html: str) -> dict | None:
    """Extract and parse __NEXT_DATA__ JSON from an HTML page."""
    m = re.search(r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.+?)</script>',
                  html, re.DOTALL)
    if not m:
        # Looser fallback
        m = re.search(r'__NEXT_DATA__[^>]*>(.+?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class SkmDkAktueltCrawler(BaseCrawler):
    """Crawler for SKM Pressemeddelelser (skm.dk press releases)."""

    site_id = "skm-dk-aktuelt"
    site_name = "Custom: skm-dk-aktuelt"
    base_url = "https://skm.dk"

    # -----------------------------------------------------------------------
    # Discovery helpers
    # -----------------------------------------------------------------------

    def _get_sitemap_urls(self) -> list[str]:
        """Parse sitemap.xml and return all press-release URLs (newest-first)."""
        raw = _curl_get(_SITEMAP_URL)
        if not raw:
            print("[skm-dk-aktuelt] Failed to fetch sitemap.xml")
            return []
        urls = re.findall(
            r"<loc>(https://skm\.dk/aktuelt/presse-nyheder/pressemeddelelser/[^<]+)</loc>",
            raw,
        )
        print(f"[skm-dk-aktuelt] Sitemap: found {len(urls)} press-release URLs")
        return urls

    def _get_build_id(self) -> str | None:
        """Detect the current Next.js buildId from the listing page."""
        raw = _curl_get(_LISTING_URL)
        if not raw:
            return None
        data = _extract_next_data(raw)
        if data:
            bid = data.get("buildId")
            if bid:
                return bid
        # Fallback: grep for buildId in raw HTML
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', raw)
        return m.group(1) if m else None

    # -----------------------------------------------------------------------
    # Detail fetching
    # -----------------------------------------------------------------------

    def _fetch_detail_json(self, url: str, build_id: str) -> dict | None:
        """Fetch /_next/data/{build_id}/{path}.json — returns pageProps dict."""
        path = url.replace(_BASE, "")          # e.g. /aktuelt/.../slug
        json_url = f"{_BASE}/_next/data/{build_id}{path}.json"
        raw = _curl_get(json_url)
        if not raw or not raw.strip().startswith("{"):
            return None
        try:
            data = json.loads(raw)
        except Exception:
            return None
        # Next.js data API returns {"pageProps": {...}}
        return data.get("pageProps")

    def _fetch_detail_html(self, url: str) -> dict | None:
        """Fallback: fetch the full HTML page and parse __NEXT_DATA__."""
        raw = _curl_get(url)
        if not raw:
            return None
        data = _extract_next_data(raw)
        if not data:
            return None
        # HTML page wraps under props.pageProps
        return (data.get("props") or {}).get("pageProps")

    def _parse_page_props(self, page_props: dict) -> dict | None:
        """Extract a paper-dict from Next.js pageProps. Returns None on failure."""
        content = page_props.get("content") or {}
        page = content.get("page") or {}

        title = (
            (page.get("properties") or {}).get("heading")
            or page.get("name")
            or ""
        ).strip()
        if not title:
            return None

        props = page.get("properties") or {}

        # Abstract: teaser + stripped body text
        teaser = (props.get("teaser") or "").strip()
        markup = ""
        text_block = props.get("text")
        if isinstance(text_block, dict):
            markup = _strip_tags(text_block.get("markup") or "").strip()
        abstract_parts = []
        if teaser:
            abstract_parts.append(teaser)
        if markup and markup != teaser:
            abstract_parts.append(markup)
        abstract = "\n\n".join(abstract_parts).strip()

        # Dates
        date_raw = props.get("date") or page.get("createDate") or ""
        published_date = _parse_date(date_raw)
        create_raw = page.get("createDate") or ""
        listed_date = _parse_date(create_raw)

        # IDs
        page_id = page.get("id") or ""
        url_path = page.get("url") or ""
        full_url = _BASE + url_path if url_path else ""
        slug = url_path.rstrip("/").split("/")[-1] if url_path else ""

        # Tags → keywords
        tags_raw = props.get("tags") or []
        tag_names = [t.get("name") for t in tags_raw if isinstance(t, dict) and t.get("name")]
        keywords = ",".join(tag_names)

        # Category from types
        types_raw = props.get("types") or []
        category = ""
        if types_raw and isinstance(types_raw[0], dict):
            category = types_raw[0].get("name") or ""

        # Check for any PDF links in the text markup
        pdf_url = None
        original_filename = None
        if isinstance(text_block, dict):
            raw_markup = text_block.get("markup") or ""
            pdf_m = re.search(r'href="([^"]*\.pdf[^"]*)"', raw_markup, re.IGNORECASE)
            if pdf_m:
                pdf_url = pdf_m.group(1)
                if not pdf_url.startswith("http"):
                    pdf_url = _BASE + pdf_url
                original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0] or None

        # metadata: all native fields not mapped above
        subject_raw = props.get("subject") or []
        subject_names = [
            s.get("name") for s in subject_raw
            if isinstance(s, dict) and s.get("name")
        ]

        metadata = {
            "node_id": page.get("id"),
            "documentType": page.get("documentType"),
            "createDate": page.get("createDate"),
            "lastUpdated": page.get("lastUpdated"),
            "subject": subject_names,
            "culture": page.get("culture"),
            "slug": slug,
        }

        return {
            "site_id": self.site_id,
            "external_id": page_id,
            "post_number": page_id,          # UUID — no numeric ID on this site
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "url": full_url or None,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "keywords": keywords,
            "category": category,
            "authors": None,
            "publisher": "Skatteministeriet",
            "department": None,
            "journal": None,
            "doi": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # -----------------------------------------------------------------------
    # Main crawl
    # -----------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl SKM pressemeddelelser.

        Args:
            limit: Max number of records to save. None = unlimited.

        Returns:
            Number of records saved.
        """
        limit_int = int(limit) if limit is not None else None
        limit_display = str(limit_int) if limit_int is not None else "∞"

        start_wall = datetime.now(timezone.utc)

        # 1. Discover URLs from sitemap
        urls = self._get_sitemap_urls()
        if not urls:
            print("[skm-dk-aktuelt] No URLs discovered. Aborting.")
            return 0

        # 2. Get buildId
        build_id = self._get_build_id()
        if build_id:
            print(f"[skm-dk-aktuelt] buildId: {build_id}")
        else:
            print("[skm-dk-aktuelt] Warning: could not detect buildId; will use HTML fallback")

        saved = 0
        seen_urls: set[str] = set()

        for idx, url in enumerate(urls):
            # Wall-clock budget
            elapsed_min = (datetime.now(timezone.utc) - start_wall).total_seconds() / 60
            if elapsed_min >= _WALL_MINUTES:
                print(f"[skm-dk-aktuelt] Wall-clock budget ({_WALL_MINUTES}m) reached after {saved} saves. Stopping.")
                break

            if limit_int is not None and saved >= limit_int:
                break

            # Safety cap (spec requirement: log at 200)
            if idx >= _MAX_PAGES * 10:          # 2000 items cap
                print(f"[skm-dk-aktuelt] Safety item cap reached. Stopping.")
                break

            # URL dedup
            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Progress log every 10 items
            if idx > 0 and idx % 10 == 0:
                print(f"[skm-dk-aktuelt] item {idx}: saved {saved}/{limit_display}")

            # Rate limit
            if idx > 0:
                time.sleep(_RATE_SLEEP)

            # Per-item isolation
            try:
                # Primary: _next/data JSON endpoint
                page_props = None
                if build_id:
                    page_props = self._fetch_detail_json(url, build_id)
                    if page_props is None:
                        # buildId may have changed — refresh it
                        new_bid = self._get_build_id()
                        if new_bid and new_bid != build_id:
                            build_id = new_bid
                            print(f"[skm-dk-aktuelt] buildId refreshed to {build_id}")
                            page_props = self._fetch_detail_json(url, build_id)

                # Fallback: full HTML parse
                if page_props is None:
                    page_props = self._fetch_detail_html(url)

                if page_props is None:
                    print(f"[skm-dk-aktuelt] item {idx}: could not fetch {url[:80]}")
                    continue

                paper = self._parse_page_props(page_props)
                if paper is None:
                    print(f"[skm-dk-aktuelt] item {idx}: parse failed for {url[:80]}")
                    continue

                # Ensure URL is set
                if not paper.get("url"):
                    paper["url"] = url

                # Skip items with very short abstract
                abstract = paper.get("abstract") or ""
                if len(abstract) < 50:
                    print(f"[skm-dk-aktuelt] item {idx}: abstract too short ({len(abstract)}c), skipping: {url[-60:]}")
                    continue

                self._save_paper(paper)
                saved += 1
                title_short = (paper.get("title") or "")[:55]
                print(f"[skm-dk-aktuelt] saved {saved}/{limit_display}: {title_short}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[skm-dk-aktuelt] item {idx} failed: {exc}")
                continue

        print(f"[skm-dk-aktuelt] Done. Total saved: {saved}")
        return saved
