# -*- coding: utf-8 -*-
"""Crawler for environment.govt.nz publications.

The live site is behind Incapsula WAF (JS challenge, blocks curl and headless
browsers).  This crawler instead enumerates publication URLs from the Wayback
Machine CDX API and fetches each archived detail page from web.archive.org.
That gives full server-rendered HTML without any bot-protection challenges.
"""

from __future__ import annotations

import json
import os
import re
import time
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler

# Wayback Machine rewrite prefix pattern (strips injected archive prefix)
_WB_PREFIX_RE = re.compile(
    r'https?://web\.archive\.org/web/\d+[a-z_]?/'
)

# Valid publication slug: lowercase letters, digits, hyphens
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9\-]*$')

_PARSERS = ["html5lib", "lxml", "html.parser"]


def _make_soup(html: str):
    from bs4 import BeautifulSoup
    for parser in _PARSERS:
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


class EnvironmentGovtNzPublicationsCrawler(BaseCrawler):
    site_id = "environment-govt-nz-publications"
    site_name = "Custom: environment-govt-nz-publications"
    base_url = "https://environment.govt.nz"

    _CDX_API = "https://web.archive.org/cdx/search/cdx"
    _WAYBACK = "https://web.archive.org/web"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _fetch(self, url: str, retries: int = 3) -> str | None:
        """GET url via requests session; return text or None on failure."""
        for attempt in range(retries):
            wait = [1, 3, 9][min(attempt, 2)]
            try:
                resp = self._session.get(url, timeout=60)
                resp.raise_for_status()
                return resp.content.decode("utf-8", errors="replace")
            except Exception as exc:
                print(
                    f"[{self.site_id}] fetch error "
                    f"(attempt {attempt + 1}/{retries}) {url}: {exc}"
                )
                if attempt < retries - 1:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # CDX enumeration
    # ------------------------------------------------------------------

    def _cdx_publication_urls(self) -> list[tuple[str, str]]:
        """Return list of (original_url, timestamp) for all publication detail
        pages, sourced from the Wayback Machine CDX API.
        """
        print(f"[{self.site_id}] Querying CDX API for publication URLs…")
        qs = (
            "url=environment.govt.nz/publications/*"
            "&output=json"
            "&filter=statuscode:200"
            "&collapse=urlkey"
            "&fl=original,timestamp"
            "&limit=50000"
        )
        raw = self._fetch(f"{self._CDX_API}?{qs}")
        if not raw:
            print(f"[{self.site_id}] CDX API returned no data")
            return []

        try:
            rows = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"[{self.site_id}] CDX JSON parse error: {exc}")
            return []

        results: list[tuple[str, str]] = []
        for row in rows[1:]:  # row 0 is the header ["original","timestamp"]
            if len(row) < 2:
                continue
            orig, ts = row[0], row[1]
            # Skip if query-string or fragment present
            if "?" in orig or "#" in orig:
                continue
            path = urlparse(orig).path  # e.g. /publications/some-title/
            parts = path.strip("/").split("/")
            # Must be exactly ["publications", "<slug>"]
            if len(parts) != 2 or parts[0] != "publications":
                continue
            slug = parts[1]
            if not slug or not _SLUG_RE.match(slug):
                continue
            results.append((orig, ts))

        print(f"[{self.site_id}] CDX: {len(results)} unique publication URLs")
        return results

    # ------------------------------------------------------------------
    # Detail-page parser
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, orig_url: str) -> dict | None:
        """Parse a publication detail page. Returns field dict or None."""
        soup = _make_soup(html)
        if soup is None:
            return None

        # --- Title ---
        title: str | None = None
        h1 = soup.find("h1")
        if h1:
            span = h1.find("span")
            if span:
                title = span.get_text(" ", strip=True)
            else:
                title = h1.get_text(" ", strip=True)
        if not title:
            for attr_name, attr_val in [
                ("name", "twitter:title"),
                ("property", "og:title"),
            ]:
                m = soup.find("meta", {attr_name: attr_val})
                if m and m.get("content"):
                    title = m["content"].strip()
                    break
        if not title:
            tt = soup.find("title")
            if tt:
                title = tt.get_text(strip=True).split("|")[0].strip()

        # --- Published date (ISO YYYY-MM-DD) ---
        published_date: str | None = None
        mp = soup.find("meta", {"property": "article:published_time"})
        if mp and mp.get("content"):
            published_date = mp["content"][:10]
        if not published_date:
            t = soup.find("time", datetime=True)
            if t:
                published_date = t.get("datetime", "")[:10]

        # --- Abstract (intro paragraphs under publication-pdf-section__intro) ---
        abstract: str | None = None
        def _has_intro(c):
            if isinstance(c, list):
                return "publication-pdf-section__intro" in " ".join(c)
            return "publication-pdf-section__intro" in str(c)

        intro = soup.find("div", class_=_has_intro)
        if intro:
            abstract = intro.get_text(" ", strip=True)

        # --- PDF URL ---
        pdf_url: str | None = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Strip Wayback rewrite prefix if present
            href_clean = _WB_PREFIX_RE.sub("", href)
            if ".pdf" in href_clean.lower():
                if href_clean.startswith("/"):
                    pdf_url = f"https://environment.govt.nz{href_clean}"
                elif href_clean.startswith("http"):
                    pdf_url = href_clean
                else:
                    pdf_url = f"https://environment.govt.nz/{href_clean.lstrip('/')}"
                break

        # --- Categories / tags ---
        category_parts: list[str] = []
        tag_ul = soup.find("ul", {"aria-label": "Article tags"})
        if tag_ul:
            for a in tag_ul.find_all("a", class_="tag"):
                t = a.get_text(strip=True)
                if t and t.lower() != "publication":
                    category_parts.append(t)

        # --- Reference number (e.g. "CR 610") ---
        reference: str | None = None
        def _has_metadata(c):
            if isinstance(c, list):
                return "metadata" in c
            return c == "metadata"

        for p in soup.find_all("p", class_=_has_metadata):
            txt = p.get_text(" ", strip=True)
            if "Reference:" in txt:
                reference = txt.replace("Reference:", "").strip()
                break

        # Clean the original URL (remove Wayback prefix if present)
        clean_url = _WB_PREFIX_RE.sub("", orig_url)
        if not clean_url.startswith("http"):
            clean_url = f"https://environment.govt.nz{clean_url}"

        slug = urlparse(clean_url).path.rstrip("/").rsplit("/", 1)[-1]

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "url": clean_url,
            "pdf_url": pdf_url,
            "category": "; ".join(category_parts) if category_parts else None,
            "external_id": slug,
            "post_number": slug,
            "reference": reference,
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25-minute wall-clock budget

        # Step 1: enumerate all publication URLs from CDX
        entries = self._cdx_publication_urls()
        if not entries:
            print(f"[{self.site_id}] No publication URLs found; aborting")
            return 0

        limit_or_inf = limit if limit is not None else float("inf")
        seen_urls: set[str] = set()
        saved = 0
        pages = 0

        for idx, (orig_url, timestamp) in enumerate(entries):
            # Respect limit
            if saved >= limit_or_inf:
                break

            # Wall-clock safety cap
            if time.time() - start_time > max_seconds:
                print(
                    f"[{self.site_id}] 25-minute budget reached after "
                    f"{idx} items; stopping"
                )
                break

            # URL dedup
            if orig_url in seen_urls:
                continue
            seen_urls.add(orig_url)
            pages += 1

            # Progress every 10 pages
            if pages % 10 == 0:
                print(
                    f"[{self.site_id}] page {pages}: "
                    f"saved {saved}/{limit_or_inf}"
                )

            wayback_url = f"{self._WAYBACK}/{timestamp}/{orig_url}"

            try:
                html = self._fetch(wayback_url)
                if not html:
                    print(f"[{self.site_id}] empty response for {wayback_url}")
                    continue

                # Sanity-check: Wayback should never return Incapsula for
                # archived pages, but log and skip just in case
                if "Incapsula" in html and len(html) < 3000:
                    print(
                        f"[{self.site_id}] bot-block response for "
                        f"{wayback_url}, skipping"
                    )
                    continue

                data = self._parse_detail(html, orig_url)
                if not data:
                    print(
                        f"[{self.site_id}] parse failed for {orig_url}, "
                        "skipping"
                    )
                    continue

                if not data.get("title"):
                    print(
                        f"[{self.site_id}] no title for {orig_url}, skipping"
                    )
                    continue

                abstract = data.get("abstract") or ""
                if len(abstract) < 50:
                    print(
                        f"[{self.site_id}] abstract too short "
                        f"({len(abstract)} chars) for {orig_url}, skipping"
                    )
                    continue

                # Build metadata dict
                meta: dict = {}
                if data.get("reference"):
                    meta["reference"] = data["reference"]
                if data.get("published_date"):
                    meta["posted_date"] = data["published_date"]

                # Extract original filename from PDF URL
                original_filename: str | None = None
                if data.get("pdf_url"):
                    fn = data["pdf_url"].split("/")[-1].split("?")[0]
                    if fn:
                        original_filename = fn

                self._save_paper({
                    "site_id": self.site_id,
                    "external_id": data["external_id"],
                    "post_number": data["post_number"],
                    "title": data["title"],
                    "abstract": abstract,
                    "published_date": data["published_date"],
                    "listed_date": data["published_date"],
                    "url": data["url"],
                    "pdf_url": data["pdf_url"],
                    "category": data["category"],
                    "publisher": "Ministry for the Environment",
                    "original_filename": original_filename,
                    "metadata": json.dumps(meta) if meta else None,
                })
                saved += 1
                time.sleep(self._delay)

            except KeyboardInterrupt:
                print(
                    f"[{self.site_id}] Interrupted by user; "
                    f"saved {saved} so far"
                )
                break
            except Exception as exc:
                print(f"[{self.site_id}] item {orig_url} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Saved {saved} publications.")
        return saved
