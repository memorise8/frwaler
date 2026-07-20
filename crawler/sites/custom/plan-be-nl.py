# -*- coding: utf-8 -*-
"""Federaal Planbureau (plan.be) NL publications crawler.

Starting URL : https://www.plan.be/nl/publicaties
Discovery    : XML sitemaps (sitemap.xml?page=1/2/3) — 1460+ NL publications.
               The main list page blocks ?page>0, so the sitemap is used instead.
Detail pages : plain curl — no JavaScript needed for individual publication pages.
"""

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_BASE = "https://www.plan.be"
_SITEMAP_BASE = f"{_BASE}/sitemap.xml"
_SITEMAP_PAGES = 3  # sitemap.xml?page=1..3


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    raise RuntimeError("No HTML parser available (install html5lib or lxml)")


# ---------------------------------------------------------------------------
# Network helper
# ---------------------------------------------------------------------------

_CURL_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, *, timeout: int = 30, retries: int = 3) -> str | None:
    """Fetch URL via curl with TLS-max 1.3 and retry/backoff."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", f"User-Agent: {_CURL_UA}",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: nl-NL,nl;q=0.9,en;q=0.7",
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
                print(f"[plan-be-nl] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[plan-be-nl] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[plan-be-nl] curl failed after {retries} attempts "
                      f"for {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Date parsing
# ---------------------------------------------------------------------------

def _parse_date(raw: str) -> str:
    """Convert 'DD/MM/YYYY' (or ISO) → 'YYYY-MM-DD'. Returns '' on failure."""
    if not raw:
        return ""
    raw = raw.strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d %B %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------

def _fetch_sitemap_urls() -> list:
    """Return all NL publication URLs from sitemaps (pages 1-3)."""
    urls = []
    for page in range(1, _SITEMAP_PAGES + 1):
        raw = _curl_get(f"{_SITEMAP_BASE}?page={page}")
        if not raw:
            print(f"[plan-be-nl] sitemap page {page} failed, skipping")
            continue
        found = re.findall(
            r"<loc>(https://www\.plan\.be/nl/publicaties/[^<]+)</loc>", raw
        )
        print(f"[plan-be-nl] sitemap page {page}: {len(found)} NL publication URLs")
        urls.extend(found)
    return urls


# ---------------------------------------------------------------------------
# Detail page parser
# ---------------------------------------------------------------------------

def _parse_detail(html: str, url: str) -> dict | None:
    """Parse a single publication detail page.

    Returns a paper dict or None if the page cannot be parsed.
    """
    try:
        soup = _make_soup(html)
    except Exception as exc:
        print(f"[plan-be-nl] soup construction failed for {url}: {exc}")
        return None

    # --- Title ---
    h1 = soup.find("h1")
    title = h1.get_text(separator=" ", strip=True) if h1 else ""
    if not title:
        print(f"[plan-be-nl] no title found for {url}")
        return None

    # --- Abstract / body ---
    body_div = soup.find("div", class_=lambda c: c and "field--name-body" in c)
    abstract = body_div.get_text(separator=" ", strip=True) if body_div else ""

    # --- Posted/listed date (DD/MM/YYYY) ---
    date_div = soup.find("div", class_=lambda c: c and "field--node-post-date" in c)
    raw_date = date_div.get_text(strip=True) if date_div else ""
    pub_date = _parse_date(raw_date)

    # --- Authors ---
    author_tags = soup.find_all("h4", class_=lambda c: c and "c-author__name" in c)
    authors_list = [t.get_text(strip=True) for t in author_tags if t.get_text(strip=True)]
    authors = "; ".join(authors_list) if authors_list else None

    # --- PDFs: prefer NL PDF ---
    all_pdfs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".pdf" in href.lower() and "/files/" in href:
            full = href if href.startswith("http") else f"{_BASE}{href}"
            if full not in all_pdfs:
                all_pdfs.append(full)

    pdf_url = None
    nl_pdfs = [p for p in all_pdfs if re.search(r"[_\-]NL[_\-.]", p, re.IGNORECASE)]
    if nl_pdfs:
        pdf_url = nl_pdfs[0]
    elif all_pdfs:
        pdf_url = all_pdfs[0]

    original_filename = os.path.basename(pdf_url.split("?")[0]) if pdf_url else None

    # --- Themes / keywords ---
    theme_tags = soup.select(".field--name-field-theme a")
    themes = list(dict.fromkeys(
        t.get_text(strip=True) for t in theme_tags if t.get_text(strip=True)
    ))
    keywords = ", ".join(themes) if themes else None

    # --- Category / publication type ---
    type_div = soup.find("div", class_=lambda c: c and "field--name-field-publication-type" in c)
    if not type_div:
        type_div = soup.find("div", class_=lambda c: c and "c-card__classification" in c)
    category = type_div.get_text(strip=True) if type_div else None

    # --- external_id and post_number from slug ---
    slug = url.rstrip("/").split("/")[-1]
    num_match = re.search(r"-(\d+)$", slug)
    post_number = num_match.group(1) if num_match else slug

    # --- Metadata: extra raw fields ---
    metadata = {
        "posted_date": raw_date,
        "slug": slug,
        "all_pdf_urls": all_pdfs,
        "themes": themes,
    }
    if category:
        metadata["category"] = category

    return {
        "external_id": slug,
        "post_number": post_number,
        "title": title,
        "abstract": abstract,
        "published_date": pub_date,
        "posted_date": pub_date,
        "listed_date": pub_date,
        "authors": authors,
        "publisher": "Federaal Planbureau",
        "url": url,
        "pdf_url": pdf_url,
        "keywords": keywords,
        "category": category,
        "original_filename": original_filename,
        "metadata": json.dumps(metadata, ensure_ascii=False),
    }


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class PlanBeNlCrawler(BaseCrawler):
    """Crawler for Federaal Planbureau (plan.be) Dutch-language publications."""

    site_id = "plan-be-nl"
    site_name = "Custom: plan-be-nl"
    base_url = "https://www.plan.be"

    def crawl(self, limit=None):
        """Crawl plan.be NL publications.

        Returns the number of newly saved documents.
        """
        limit_eff = limit if limit is not None else float("inf")
        saved = 0
        seen_urls = set()
        start_time = time.time()
        MAX_WALL_SECONDS = 25 * 60  # 25-minute hard budget

        # Step 1 — collect all URLs from sitemaps
        print("[plan-be-nl] Fetching publication URLs from sitemaps...")
        all_urls = _fetch_sitemap_urls()
        if not all_urls:
            print("[plan-be-nl] No URLs found in sitemaps — aborting")
            return 0
        print(f"[plan-be-nl] Total sitemap URLs: {len(all_urls)}")

        # Step 2 — fetch + parse each detail page
        for i, url in enumerate(all_urls):
            if saved >= limit_eff:
                break

            if url in seen_urls:
                continue
            seen_urls.add(url)

            # Wall-clock budget check
            if time.time() - start_time > MAX_WALL_SECONDS:
                print(f"[plan-be-nl] 25-min wall-clock budget reached, "
                      f"stopping at {saved} saved")
                break

            # Progress log every 10 items
            if i > 0 and i % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[plan-be-nl] item {i}: saved {saved}/{lim_str}")

            try:
                html = _curl_get(url)
                if not html:
                    print(f"[plan-be-nl] skipping {url}: fetch failed after retries")
                    continue

                paper = _parse_detail(html, url)
                if not paper:
                    print(f"[plan-be-nl] skipping {url}: parse returned None")
                    continue

                abstract = paper.get("abstract") or ""
                if len(abstract) < 100:
                    print(f"[plan-be-nl] skipping {url}: abstract too short "
                          f"({len(abstract)} chars)")
                    continue

                self._save_paper(paper)
                saved += 1

            except KeyboardInterrupt:
                print(f"[plan-be-nl] Interrupted by user at item {i}, "
                      f"saved {saved} so far")
                raise
            except Exception as exc:
                print(f"[plan-be-nl] item {url} failed: {exc}")
                continue

            time.sleep(1.0)

        print(f"[plan-be-nl] Done: saved {saved} documents")
        return saved
