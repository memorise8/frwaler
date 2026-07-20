# -*- coding: utf-8 -*-
"""NINA Older-series crawler — nina.no HTML listing.

Starting URL : https://www.nina.no/english/Publications/Older-NINA-series

The page is a single static HTML document with year-group headers (<h3>YYYY</h3>)
and individual citations separated by <hr>.  There is no pagination API.
Abstract = full citation text (100–500 chars for real entries; short ones skipped).
"""

import hashlib
import json
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

_LIST_URL = "https://www.nina.no/english/Publications/Older-NINA-series"
_PUBLISHER = "Norwegian Institute for Nature Research (NINA)"
_MIN_ABSTRACT = 100   # citations shorter than this are incomplete stubs
_MAX_PAGES = 200      # safety cap (single HTML page = 1; kept for protocol)
_WALL_BUDGET = 25 * 60


def _make_soup(html):
    """BeautifulSoup with fallback parsers: html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _get_text(node):
    return node.get_text() if hasattr(node, "get_text") else str(node)


class NINAOlderSeriesCrawler(BaseCrawler):
    """Crawler for NINA Older-series HTML publications listing."""

    site_id = "nina-no-english"
    site_name = "Custom: nina-no-english"
    base_url = "https://www.nina.no"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, *, timeout=45):
        """GET via curl with exponential backoff retries. Returns str or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            wait = (1, 3, 9)[attempt]
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
                if res.stdout:
                    return res.stdout.decode("utf-8", errors="replace")
                if attempt < 2:
                    print(f"[{self.site_id}] empty response (attempt {attempt+1}/3), "
                          f"retry in {wait}s — {url[:70]}")
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] timeout (attempt {attempt+1}/3) — {url[:70]}")
                if attempt < 2:
                    time.sleep(wait)
            except Exception as exc:
                print(f"[{self.site_id}] curl error (attempt {attempt+1}/3): {exc}")
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _parse_listing_page(self, html):
        """Parse the Older-series HTML page and return list of raw entry dicts."""
        soup = _make_soup(html)
        if soup is None:
            print(f"[{self.site_id}] failed to parse HTML (all parsers failed)")
            return []

        h3s = soup.find_all("h3")
        pub_h3s = [h for h in h3s if re.match(r"^\d{4}$", h.get_text(strip=True))]
        if not pub_h3s:
            print(f"[{self.site_id}] no year-header sections found in page")
            return []

        raw_entries = []
        for h3 in pub_h3s:
            year = h3.get_text(strip=True)
            siblings = []
            sib = h3.next_sibling
            while sib:
                if hasattr(sib, "name") and sib.name == "h3":
                    break
                siblings.append(sib)
                sib = sib.next_sibling

            # Group siblings by <hr> separator
            group = []
            for s in siblings:
                if hasattr(s, "name") and s.name == "hr":
                    if group:
                        raw_entries.append((year, group))
                    group = []
                else:
                    group.append(s)
            if group:
                raw_entries.append((year, group))

        entries = []
        for year, parts in raw_entries:
            entry = self._parse_single_entry(year, parts)
            if entry:
                entries.append(entry)
        return entries

    def _parse_single_entry(self, year, parts):
        """Convert a list of sibling nodes into a citation entry dict."""
        full_text = "".join(_get_text(p) for p in parts).strip()
        if not full_text:
            return None

        # Extract PDF URL and title from first <a> tag
        pdf_url = None
        title = None
        for p in parts:
            if not hasattr(p, "name") or p.name != "a":
                continue
            href = (p.get("href") or "").strip()
            if href:
                pdf_url = href if href.startswith("http") else f"{self.base_url}{href}"
            link_text = p.get_text(strip=True)
            if link_text:
                title = link_text
            break

        # When no hyperlink, extract title from plain citation text
        # Format: "Authors. YEAR. Title. [Extra]. - NINA Fagrapport NN. NN pp."
        if not title:
            # Match text between ". YYYY. " and " - NINA" (series reference)
            m = re.search(r"\.\s*\d{4}\.\s+(.+?)(?:\s*[.–-]+\s*NINA\s|\s*$)", full_text, re.DOTALL)
            if m:
                candidate = m.group(1).strip().rstrip(".")
                if candidate:
                    title = candidate[:300]

        if not title:
            title = full_text[:200]

        # Authors: text before ". YYYY. "
        authors = None
        m = re.match(r"^(.+?)\.\s*\d{4}[\.\s]", full_text, re.DOTALL)
        if m:
            authors = re.sub(r"\s+", " ", m.group(1)).strip()

        # Report series and number: "NINA Fagrapport 75", "NINA Temahefte 12", etc.
        series_name = None
        report_num = None
        m = re.search(r"NINA\s+(\w+)\s+(\d+)", full_text)
        if m:
            series_name = m.group(1).strip()
            report_num = m.group(2).strip()

        # stable external_id: series-number pair when available, else MD5 of text
        if series_name and report_num:
            external_id = f"nina-{series_name.lower()}-{report_num}"
        else:
            external_id = hashlib.md5(full_text.encode("utf-8", errors="replace")).hexdigest()[:16]

        # Original filename from PDF URL
        original_filename = None
        if pdf_url:
            original_filename = pdf_url.rstrip("/").split("/")[-1] or None

        meta = {
            "posted_date": None,
            "originalFilename": original_filename,
            "series": series_name,
            "report_number": report_num,
            "citation": full_text,
        }

        return {
            "external_id": external_id,
            "post_number": report_num,          # numeric string for incremental stop
            "title": title,
            "abstract": full_text,              # citation IS the abstract for older series
            "authors": authors,
            "publisher": _PUBLISHER,
            "published_date": year,             # year only (YYYY)
            "listed_date": None,
            "url": pdf_url or _LIST_URL,        # prefer PDF URL as detail URL
            "pdf_url": pdf_url,
            "keywords": None,
            "category": series_name,
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        seen_ids = set()
        limit_str = str(limit) if limit is not None else "∞"

        # ---- Phase 1: fetch the single listing page ----
        print(f"[{self.site_id}] fetching {_LIST_URL}")
        html = self._curl_get(_LIST_URL)
        if not html:
            print(f"[{self.site_id}] failed to fetch listing page")
            return 0

        # ---- Phase 2: parse all entries from the HTML ----
        entries = self._parse_listing_page(html)
        if not entries:
            print(f"[{self.site_id}] no entries found — page structure may have changed")
            return 0

        total = len(entries)
        print(f"[{self.site_id}] found {total} candidate entries on listing page")

        # Model entries as a single "page" for protocol compliance
        page = 1
        processed_on_page = 0

        for i, entry in enumerate(entries, 1):
            try:
                # Wall-clock budget
                if time.time() - start_time > _WALL_BUDGET:
                    print(f"[{self.site_id}] 25-min wall budget reached, stopping (saved={saved})")
                    break

                if limit is not None and saved >= limit:
                    break

                # URL/ID deduplication
                ext_id = entry.get("external_id") or ""
                if ext_id in seen_ids:
                    continue
                seen_ids.add(ext_id)

                # Skip entries with short abstracts
                abstract = entry.get("abstract", "")
                if len(abstract) < _MIN_ABSTRACT:
                    print(f"[{self.site_id}] skip entry {i}: abstract {len(abstract)} chars "
                          f"(< {_MIN_ABSTRACT}) — {abstract[:40]!r}")
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": entry["external_id"],
                    "post_number": entry.get("post_number"),
                    "title": entry["title"],
                    "abstract": abstract,
                    "published_date": entry.get("published_date"),
                    "listed_date": entry.get("listed_date"),
                    "authors": entry.get("authors"),
                    "publisher": entry.get("publisher"),
                    "journal": None,
                    "url": entry.get("url"),
                    "pdf_url": entry.get("pdf_url"),
                    "keywords": entry.get("keywords"),
                    "category": entry.get("category"),
                    "doi": entry.get("doi"),
                    "original_filename": entry.get("original_filename"),
                    "metadata": entry.get("metadata"),
                }

                self._save_paper(paper)
                saved += 1
                processed_on_page += 1
                print(f"[{self.site_id}] saved {saved}/{limit_str}: {entry['title'][:60]}")

                # Progress every 10 "virtual pages" (every 10 entries since there's 1 real page)
                if saved % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

                time.sleep(self._delay * 0.1)  # light delay; no per-detail fetch needed

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {i} failed: {exc}")
                continue

        # Detect "0 new records" end condition (only 1 real page)
        if processed_on_page == 0:
            print(f"[{self.site_id}] page {page} returned 0 new records — done")

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
