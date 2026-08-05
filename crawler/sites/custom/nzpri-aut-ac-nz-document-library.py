# -*- coding: utf-8 -*-
"""Crawler for NZPRI (AUT) Document Library.

Strategy:
  - Fetch the Squiz Matrix "api" design endpoint which returns ALL documents
    as a single HTML fragment (no pagination needed).
  - Split on <hr> separators; each chunk contains one <h2> title and an
    optional <div id="component_XXXXX"> or <div id="content_container_XXXXX">
    with the abstract and PDF links.
  - post_number = the numeric Squiz asset ID embedded in the component div.
    For working papers without a component div (e.g. "26/03 Title..."),
    derive a synthetic key from the working-paper code.

NOTE (2026-08): nzpri.aut.ac.nz now sits behind a Cloudflare JS challenge
("Just a moment...") that returns 403 to plain curl/requests. Fetches go
through crawler.stealth_fetcher.StealthSession (curl_cffi -> playwright
fallback) instead of raw curl.
"""

from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler
from crawler.stealth_fetcher import StealthSession

_API_URL = (
    "https://nzpri.aut.ac.nz/document-library/document-library-search"
    "?SQ_DESIGN_NAME=api&tags=&department="
)
_BASE = "https://nzpri.aut.ac.nz"
_ANCHOR_BASE = "https://nzpri.aut.ac.nz/document-library/all-document-library"

_MIN_ABSTRACT = 100


def _bs4(html: str):
    """BeautifulSoup with html5lib → lxml → html.parser fallback."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("no working HTML parser available")




def _date_from_title(title: str) -> str | None:
    """Extract date from working-paper code like '26/03 ...' → '2026-03'."""
    m = re.match(r"^(\d{2})/(\d{2})\s", title)
    if m:
        yr = int(m.group(1))
        mo = int(m.group(2))
        year = 2000 + yr if yr < 50 else 1900 + yr
        return f"{year}-{mo:02d}"
    return None


def _date_from_filename(filename: str) -> str | None:
    """Try to extract ISO date from a PDF filename."""
    # YYYY_MM_DD or YYYY-MM-DD
    m = re.search(r"(\d{4})[_-](\d{2})[_-](\d{2})(?!\d)", filename)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    # YYYY_MM or YYYY-MM  (not followed by another digit)
    m = re.search(r"(\d{4})[_-](\d{2})(?!\d)", filename)
    if m:
        yr = int(m.group(1))
        if 2000 <= yr <= 2035:
            return f"{m.group(1)}-{m.group(2)}"
    # Bare year
    m = re.search(r"(20\d{2})(?!\d)", filename)
    if m:
        return m.group(1)
    return None


def _abs_href(href: str) -> str:
    """Make a relative href absolute."""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return _BASE + href
    return urljoin(_BASE + "/", href)


class NZPRIDocumentLibraryCrawler(BaseCrawler):

    site_id = "nzpri-aut-ac-nz-document-library"
    site_name = "Custom: nzpri-aut-ac-nz-document-library"
    base_url = "https://nzpri.aut.ac.nz"

    FETCH_RETRIES = 3

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn, delay=delay)
        self._stealth = StealthSession(playwright_timeout=60)

    def _fetch(self, url):
        """GET via StealthSession (curl_cffi -> playwright fallback), with retries."""
        for attempt in range(self.FETCH_RETRIES):
            html, info = self._stealth.fetch_html(url)
            if html and info.get("final_reason") == "ok":
                return html
            print(
                f"[{self.site_id}] fetch attempt {attempt + 1}/"
                f"{self.FETCH_RETRIES} failed for {url}: {info.get('final_reason')}"
            )
            if attempt < self.FETCH_RETRIES - 1:
                time.sleep(2 * (attempt + 1))
        return None

    def crawl(self, limit=None):
        started = time.monotonic()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        saved = 0
        seen_keys: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        print(f"[{self.site_id}] Fetching document library API...")
        raw = self._fetch(_API_URL)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch API. Aborting.")
            return 0

        # Split on <hr> — each chunk after the first is one document block
        chunks = re.split(r"<hr\s*/?>", raw, flags=re.IGNORECASE)
        total_chunks = len(chunks) - 1
        print(f"[{self.site_id}] Found {total_chunks} document blocks.")

        for chunk_idx, chunk in enumerate(chunks[1:], 1):
            if limit is not None and saved >= limit:
                break

            if time.monotonic() - started > MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall-clock limit reached. Stopping.")
                break

            if chunk_idx % 10 == 0:
                print(
                    f"[{self.site_id}] page {chunk_idx}: saved {saved}/{limit_str}"
                )

            try:
                # ── title ────────────────────────────────────────────────
                h2_m = re.search(r"<h2[^>]*>(.*?)</h2>", chunk, re.DOTALL | re.IGNORECASE)
                if not h2_m:
                    continue
                title = unescape(re.sub(r"<[^>]+>", "", h2_m.group(1))).strip()
                if not title:
                    continue

                # ── component / container ID ─────────────────────────────
                cid_m = re.search(
                    r'id="((component|content_container)_(\d+))"', chunk
                )
                component_id = cid_m.group(1) if cid_m else None
                numeric_id = cid_m.group(3) if cid_m else None

                # ── post_number ──────────────────────────────────────────
                if numeric_id:
                    post_number = numeric_id
                else:
                    wp_m = re.match(r"^(\d{2})/(\d{2})\s", title)
                    post_number = f"wp{wp_m.group(1)}{wp_m.group(2)}" if wp_m else None

                # ── dedup ────────────────────────────────────────────────
                dedup_key = component_id or title
                if dedup_key in seen_keys:
                    continue
                seen_keys.add(dedup_key)

                # ── parse with BeautifulSoup ─────────────────────────────
                try:
                    soup = _bs4(chunk)
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {chunk_idx} BS4 parse failed: {exc}; continue"
                    )
                    continue

                # Content source: prefer the specific component div
                content_div = soup.find(id=component_id) if component_id else soup

                # ── abstract ─────────────────────────────────────────────
                abstract_parts: list[str] = []
                for p in content_div.find_all("p"):
                    text = p.get_text(separator=" ", strip=True)
                    if len(text) < 30:
                        continue
                    # Skip "View the …" / "Download …" / "Read the …" link labels
                    if re.match(r"^(view|download|read)\s+the\b", text, re.IGNORECASE):
                        continue
                    # Skip paragraphs whose only non-whitespace content is a single link
                    links = p.find_all("a")
                    if links and p.get_text(strip=True) == links[0].get_text(strip=True):
                        continue
                    abstract_parts.append(text)

                abstract = "\n\n".join(abstract_parts).strip()

                if len(abstract) < _MIN_ABSTRACT:
                    print(
                        f"[{self.site_id}] Skip (abstract {len(abstract)} chars): {title[:60]}"
                    )
                    continue

                # ── PDF links ─────────────────────────────────────────────
                pdf_url: str | None = None
                all_pdfs: list[dict] = []
                for a in content_div.find_all("a", href=True):
                    href = a["href"]
                    if ".pdf" in href.lower():
                        abs_href = _abs_href(href)
                        label = a.get_text(strip=True)
                        all_pdfs.append({"label": label, "url": abs_href})
                        if pdf_url is None:
                            pdf_url = abs_href

                # ── original filename ─────────────────────────────────────
                original_filename: str | None = None
                if pdf_url:
                    original_filename = pdf_url.rstrip("/").split("/")[-1].split("?")[0]

                # ── published date ────────────────────────────────────────
                published_date = _date_from_title(title)
                if not published_date and original_filename:
                    published_date = _date_from_filename(original_filename)

                # ── funder / publisher ────────────────────────────────────
                publisher: str | None = None
                for strong in content_div.find_all("strong"):
                    if "funder" in strong.get_text(strip=True).lower():
                        parent_text = strong.parent.get_text(separator=" ", strip=True)
                        fm = re.search(r"[Ff]under\(?s?\)?:?\s*(.*)", parent_text)
                        if fm:
                            publisher = fm.group(1).strip() or None
                        break

                # ── category ──────────────────────────────────────────────
                category = "working-papers" if re.match(r"^\d{2}/\d{2}\s", title) else ""

                # ── detail URL ────────────────────────────────────────────
                url = (
                    f"{_ANCHOR_BASE}#{component_id}"
                    if component_id
                    else _ANCHOR_BASE
                )

                # ── external_id ───────────────────────────────────────────
                external_id = component_id or f"title:{title[:120]}"

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": None,
                    "authors": None,
                    "publisher": publisher,
                    "department": None,
                    "journal": None,
                    "url": url,
                    "pdf_url": pdf_url,
                    "keywords": None,
                    "category": category,
                    "doi": None,
                    "original_filename": original_filename,
                    "metadata": json.dumps(
                        {
                            "posted_date": None,
                            "originalFilename": original_filename,
                            "component_id": component_id,
                            "all_pdfs": all_pdfs,
                            "category": category,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                saved += 1
                print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {chunk_idx} failed: {exc}; continue")
                continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
