# -*- coding: utf-8 -*-
"""Udenrigsministeriet (um.dk) – Økonomi, mål og resultater annual reports crawler.

Starting URL:
  https://um.dk/om-os/organisation/oekonomi-og-udbud/oekonomi-maal-og-resultater

Single static HTML page listing annual report PDFs (2002–2025).
No pagination, no detail pages — all data extracted from the list page.
"""

import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE_URL = "https://um.dk"
_LIST_URL = (
    "https://um.dk/om-os/organisation/oekonomi-og-udbud/oekonomi-maal-og-resultater"
)
_PUBLISHER = "Udenrigsministeriet"
_SITE_ID = "um-dk-om-os"
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Page-level description text used as shared context in each record's abstract.
# This is extracted from the page at crawl time, but we keep a fallback here
# in case the page structure changes.
_FALLBACK_DESCRIPTION = (
    "Udenrigsministeriets globale operationsområde og den integrerede "
    "opgavevaretagelse stiller særlige krav til både den overordnede styring "
    "i Udenrigsministeriet og det styringsmæssige samspil mellem ministeriet "
    "i København og repræsentationerne i udlandet. "
    "Udenrigsministeriets opgave er at føre regeringens politik ud i livet."
)

_RATE_SLEEP = 1.0
_MAX_PAGES = 200  # safety cap (this site is a single page, but kept for API compat)
_MAX_MINUTES = 25


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available")


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with exponential-backoff retries. Returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_USER_AGENT}",
        "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
        "-H", "Accept-Language: da-DK,da;q=0.9,en-US;q=0.8,en;q=0.7",
        "-H", f"Referer: {_BASE_URL}/",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {exc}")
    return None


def _extract_plain_paragraphs(soup, *, min_chars: int = 80) -> list:
    """Return text of paragraphs that are at least min_chars long and don't look
    like link-only paragraphs."""
    texts = []
    for p in soup.find_all("p"):
        # Skip if paragraph is just a link (no extra text)
        a_tags = p.find_all("a")
        p_text = p.get_text(separator=" ", strip=True)
        if not p_text:
            continue
        # If the paragraph text matches the anchor text(s) exactly, skip.
        anchor_texts = " ".join(a.get_text(strip=True) for a in a_tags)
        if anchor_texts and p_text == anchor_texts:
            continue
        if len(p_text) >= min_chars:
            texts.append(p_text)
    return texts


def _build_abstract(page_description: str, year: str, link_title: str) -> str:
    """Construct a per-report abstract from page context and record metadata."""
    year_line = f"Udenrigsministeriets årsrapport for {year}. "
    combined = year_line + page_description
    return combined[:2000]  # cap at 2000 chars


def _extract_media_id(path: str) -> str:
    """Extract the media hash segment from a /media/<hash>/filename.pdf path."""
    parts = [p for p in path.split("/") if p]
    # e.g. ['media', 'sswdydqe', 'udenrigsministeriet-aarsrapport-2025.pdf']
    if len(parts) >= 2:
        return parts[1]  # the hash/slug portion
    return re.sub(r"[^a-z0-9]", "-", path.lower())


def _original_filename(pdf_url: str) -> str | None:
    """Return the filename part of the PDF URL."""
    try:
        return pdf_url.rstrip("/").split("/")[-1]
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class UmDkOmOsCrawler(BaseCrawler):
    """Annual reports from Udenrigsministeriet (Danish Ministry of Foreign Affairs)."""

    site_id = _SITE_ID
    site_name = "Custom: um-dk-om-os"
    base_url = _BASE_URL

    def crawl(self, limit=None):
        """Crawl the single listing page and save each annual report PDF entry.

        Each entry corresponds to one PDF linked from the page.
        """
        deadline = time.time() + _MAX_MINUTES * 60
        saved = 0
        seen_urls: set = set()
        limit_or_inf = limit if limit is not None else float("inf")

        # ------------------------------------------------------------------
        # 1. Fetch the listing page
        # ------------------------------------------------------------------
        print(f"[{_SITE_ID}] fetching listing page: {_LIST_URL}")
        html = _curl_get(_LIST_URL)
        if not html:
            print(f"[{_SITE_ID}] FATAL: could not fetch listing page")
            return 0

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{_SITE_ID}] soup parse error on listing page: {exc}")
            return 0

        # ------------------------------------------------------------------
        # 2. Extract page-level description to use as shared abstract context
        # ------------------------------------------------------------------
        paragraphs = _extract_plain_paragraphs(soup)
        if paragraphs:
            page_description = " ".join(paragraphs)
        else:
            page_description = _FALLBACK_DESCRIPTION
        print(f"[{_SITE_ID}] page description length: {len(page_description)} chars")

        # ------------------------------------------------------------------
        # 3. Find all PDF links
        # ------------------------------------------------------------------
        pdf_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href.lower().endswith(".pdf"):
                continue
            # Build absolute URL
            if href.startswith("/"):
                url = _BASE_URL + href
            elif href.startswith("http"):
                url = href
            else:
                url = _BASE_URL + "/" + href

            if url in seen_urls:
                continue
            seen_urls.add(url)

            link_text = a.get_text(separator=" ", strip=True)
            # Extract year from link text or filename
            year_match = re.search(r"\b(20\d\d|199\d)\b", link_text) or \
                         re.search(r"\b(20\d\d|199\d)\b", href)
            year = year_match.group(1) if year_match else None

            pdf_links.append({
                "url": url,
                "link_text": link_text,
                "year": year,
                "href": href,
            })

        print(f"[{_SITE_ID}] found {len(pdf_links)} PDF links")

        if not pdf_links:
            print(f"[{_SITE_ID}] no PDF links found on page")
            return 0

        # ------------------------------------------------------------------
        # 4. Build and save records
        # ------------------------------------------------------------------
        for idx, item in enumerate(pdf_links):
            if saved >= limit_or_inf:
                break
            if time.time() > deadline:
                print(f"[{_SITE_ID}] approaching {_MAX_MINUTES}min budget — stopping")
                break

            if idx > 0 and idx % 10 == 0:
                print(f"[{_SITE_ID}] page 1: saved {saved}/{limit_or_inf}")

            try:
                pdf_url = item["url"]
                year = item["year"]
                link_text = item["link_text"]

                # Build title
                if year:
                    title = f"Udenrigsministeriets årsrapport {year}"
                else:
                    title = link_text or "Udenrigsministeriets årsrapport"

                # Build abstract from page description + year context
                abstract = _build_abstract(page_description, year or "ukendt", title)

                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] short abstract for {pdf_url} — skipping")
                    continue

                # external_id: media hash from URL path
                media_id = _extract_media_id(item["href"])
                # post_number: year string (numeric for incremental collection)
                post_number = year if year else media_id

                orig_filename = _original_filename(pdf_url)

                published_date = f"{year}-01-01" if year else None

                metadata = {
                    "link_text": link_text,
                    "media_id": media_id,
                    "href": item["href"],
                    "year": year,
                    "source_page": _LIST_URL,
                    "originalFilename": orig_filename,
                    "posted_date": published_date,
                }

                self._save_paper({
                    "site_id": _SITE_ID,
                    "external_id": media_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "url": _LIST_URL,
                    "pdf_url": pdf_url,
                    "publisher": _PUBLISHER,
                    "department": "Økonomi og udbud",
                    "authors": None,
                    "journal": None,
                    "keywords": "årsrapport,økonomi,mål og resultater,Udenrigsministeriet",
                    "category": "Årsrapport",
                    "doi": None,
                    "original_filename": orig_filename,
                    "metadata": json.dumps(metadata, ensure_ascii=False),
                })

                saved += 1
                print(f"[{_SITE_ID}] saved [{saved}] {title} — {pdf_url}")

                time.sleep(_RATE_SLEEP)

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] done — saved {saved} records")
        return saved
