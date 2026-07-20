# -*- coding: utf-8 -*-
"""Crawler for MPI NZ Wood Processing Data (mpi-govt-nz-forestry).

Target: https://www.mpi.govt.nz/forestry/forest-industry-and-workforce/
        forestry-wood-processing-data/wood-processing-data/

The site is behind Incapsula WAF — plain curl gets blocked.
Strategy: use Playwright with a homepage-first warmup to get valid session
cookies, then fetch the listing page and each document detail page within
the same browser context.
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler


_SITE_ID = "mpi-govt-nz-forestry"
_BASE_URL = "https://www.mpi.govt.nz"
_LISTING_URL = (
    "https://www.mpi.govt.nz/forestry/forest-industry-and-workforce/"
    "forestry-wood-processing-data/wood-processing-data/"
)
_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(raw: str) -> str:
    """Convert '01 Apr 2014' → '2014-04-01'.  Returns '' on failure."""
    if not raw:
        return ""
    m = re.match(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", raw.strip())
    if m:
        d, mon, y = m.groups()
        return f"{y}-{_MONTHS.get(mon.lower(), '01')}-{int(d):02d}"
    return ""


def _strip_xlsx_refs(text: str) -> str:
    """Remove [XLSX, N KB] / [PDF, N KB] noise from extracted text."""
    text = re.sub(r"\[(?:XLSX|PDF|CSV|ZIP)[^\]]*\]", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip()


class MPIForestryWoodProcessingCrawler(BaseCrawler):
    """Crawls wood-processing data documents from MPI NZ."""

    site_id = "mpi-govt-nz-forestry"
    site_name = "Custom: mpi-govt-nz-forestry"
    base_url = "https://www.mpi.govt.nz"

    # ------------------------------------------------------------------ #
    # Playwright helpers
    # ------------------------------------------------------------------ #

    def _make_pw_context(self, pw):
        """Create a Chromium browser context that bypasses Incapsula."""
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="Pacific/Auckland",
            viewport={"width": 1280, "height": 800},
            extra_http_headers={"Accept-Language": "en-NZ,en;q=0.9"},
        )
        return browser, context

    def _fetch_page(self, context, url: str, *, wait_secs: float = 4.0,
                    retries: int = 3, min_bytes: int = 5000) -> str | None:
        """Open url in a new tab, return rendered HTML or None after retries."""
        delays = [1, 3, 9]
        for attempt in range(retries):
            page = context.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=35_000)
                time.sleep(wait_secs)
                html = page.content()
                if html and len(html) >= min_bytes:
                    return html
                wait = delays[min(attempt, len(delays) - 1)]
                print(
                    f"[{_SITE_ID}] short response ({len(html) if html else 0} B) "
                    f"for {url} — retry {attempt + 1}/{retries} in {wait}s"
                )
                time.sleep(wait)
            except Exception as exc:
                wait = delays[min(attempt, len(delays) - 1)]
                print(
                    f"[{_SITE_ID}] fetch error (attempt {attempt + 1}/{retries}) "
                    f"for {url}: {exc}"
                )
                if attempt < retries - 1:
                    time.sleep(wait)
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        return None

    # ------------------------------------------------------------------ #
    # Parsing helpers
    # ------------------------------------------------------------------ #

    def _extract_docs_from_listing(self, html: str) -> list[dict]:
        """Return list of doc-info dicts from the listing page HTML."""
        soup = _make_soup(html)
        if not soup:
            return []

        main = soup.find("main") or soup.find("div", id="main") or soup.body
        if not main:
            return []

        # Build section descriptions (heading → clean text before next heading)
        headings = main.find_all(["h2", "h3", "h4"])
        sections_desc: dict[str, str] = {}
        for h in headings:
            heading_text = h.get_text(strip=True)
            parts: list[str] = []
            for sib in h.next_siblings:
                if hasattr(sib, "name") and sib.name in ("h2", "h3", "h4"):
                    break
                if hasattr(sib, "get_text"):
                    t = _strip_xlsx_refs(sib.get_text(separator=" ", strip=True))
                    # Skip lines that are just document-link text (very short or
                    # look like a title/filename)
                    if len(t) > 30:
                        parts.append(t)
            sections_desc[heading_text] = " ".join(parts)[:1200]

        main_str = str(main)

        # Collect dmsdocument <a> links (deduplicated by doc_id)
        seen_ids: set[str] = set()
        docs: list[dict] = []
        for a_tag in main.find_all("a", href=re.compile(r"/dmsdocument/\d+")):
            href = a_tag.get("href", "")
            m = re.search(r"/dmsdocument/(\d+)", href)
            if not m:
                continue
            doc_id = m.group(1)
            if doc_id in seen_ids:
                continue
            seen_ids.add(doc_id)

            full_url = href if href.startswith("http") else f"{_BASE_URL}{href}"
            title = a_tag.get("title", "") or a_tag.get_text(strip=True)
            title = re.sub(
                r"\s*-\s*download document$", "", title, flags=re.IGNORECASE
            ).strip()
            ext = (a_tag.get("data-ext") or "").upper()
            size_bytes = a_tag.get("data-size") or ""

            # Find the last heading that precedes this link in the HTML
            doc_pos = main_str.find(f"/dmsdocument/{doc_id}")
            best_section = "Wood processing data"
            best_pos = -1
            for h in headings:
                h_pos = main_str.find(str(h))
                if 0 <= h_pos < doc_pos and h_pos > best_pos:
                    best_pos = h_pos
                    best_section = h.get_text(strip=True)

            docs.append({
                "doc_id": doc_id,
                "title": title,
                "url": full_url,
                "ext": ext,
                "size_bytes": size_bytes,
                "section": best_section,
                "section_desc": sections_desc.get(best_section, ""),
            })

        return docs

    def _parse_detail(self, html: str) -> dict:
        """Extract published_date, last_updated, doc_type, description from detail page."""
        soup = _make_soup(html)
        if not soup:
            return {}

        for tag in soup(["script", "style", "nav", "header", "footer"]):
            tag.decompose()
        text = re.sub(r"\s+", " ", soup.get_text(separator=" ", strip=True))

        pub_m = re.search(r"Published\s+(\d{1,2}\s+\w{3}\s+\d{4})", text)
        upd_m = re.search(r"Last\s+updated\s+(\d{1,2}\s+\w{3}\s+\d{4})", text)

        published_date = _parse_date(pub_m.group(1)) if pub_m else ""
        last_updated = _parse_date(upd_m.group(1)) if upd_m else ""

        # Description: text between "Type <word>" and "Published|Last updated"
        desc_m = re.search(
            r"Type\s+\S+\s+(.{10,}?)(?=\s+Published\s+\d|\s+Last\s+updated\s+\d)",
            text,
            re.DOTALL,
        )
        doc_desc = re.sub(r"\s+", " ", desc_m.group(1)).strip() if desc_m else ""

        # Doc type
        type_m = re.search(r"\bType\s+(\w[\w\s]{0,40}?)(?=\s+(?:[A-Z][a-z]|Published|Last))", text)
        doc_type = type_m.group(1).strip() if type_m else ""

        return {
            "published_date": published_date,
            "last_updated": last_updated,
            "doc_desc": doc_desc,
            "doc_type": doc_type,
            "published_raw": pub_m.group(1) if pub_m else "",
            "updated_raw": upd_m.group(1) if upd_m else "",
        }

    # ------------------------------------------------------------------ #
    # Main crawl
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):  # noqa: C901
        """Crawl MPI wood-processing-data documents using Playwright.

        Uses a two-step session warmup (homepage first) to obtain valid
        Incapsula session cookies, then walks the listing page and each
        document's detail page within the same browser context.
        """
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            print(f"[{_SITE_ID}] ERROR: playwright not installed. "
                  "Run: pip install playwright && playwright install chromium")
            return 0

        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        with sync_playwright() as pw:
            browser, context = self._make_pw_context(pw)
            try:
                # ---- Step 1: Homepage warmup (gets Incapsula cookies) ----
                print(f"[{_SITE_ID}] Warming up session via homepage…")
                try:
                    page0 = context.new_page()
                    page0.goto(_BASE_URL + "/", wait_until="domcontentloaded",
                               timeout=30_000)
                    time.sleep(2)
                    page0.close()
                except Exception as exc:
                    print(f"[{_SITE_ID}] Homepage warmup error (non-fatal): {exc}")

                # ---- Step 2: Listing page ----
                print(f"[{_SITE_ID}] Fetching listing page…")
                listing_html = self._fetch_page(
                    context, _LISTING_URL, wait_secs=6, min_bytes=50_000
                )
                if not listing_html:
                    print(f"[{_SITE_ID}] Failed to load listing page. Aborting.")
                    return 0

                # ---- Step 3: Parse document list ----
                docs = self._extract_docs_from_listing(listing_html)
                if not docs:
                    print(f"[{_SITE_ID}] No documents found on listing page.")
                    return 0

                print(f"[{_SITE_ID}] Found {len(docs)} documents on listing page.")

                # ---- Step 4: Detail pages ----
                total = len(docs)
                for i, doc in enumerate(docs):
                    if limit is not None and saved >= limit:
                        break

                    # Time-budget check (25 min)
                    if time.time() - start_time > 25 * 60:
                        print(f"[{_SITE_ID}] 25-min budget reached at item {i}. Stopping.")
                        break

                    if i % 10 == 0 and i > 0:
                        print(f"[{_SITE_ID}] page 1: saved {saved}/{limit_str}")

                    doc_url = doc["url"]
                    doc_id = doc["doc_id"]
                    title = doc["title"]

                    if doc_url in seen_urls:
                        continue
                    seen_urls.add(doc_url)

                    try:
                        print(
                            f"[{_SITE_ID}] [{i + 1}/{total}] "
                            f"Fetching detail: {title[:70]}"
                        )
                        detail_html = self._fetch_page(
                            context, doc_url, wait_secs=3, min_bytes=5_000
                        )
                        if not detail_html:
                            print(f"[{_SITE_ID}] item {doc_id} failed: no detail page. Skipping.")
                            continue

                        detail = self._parse_detail(detail_html)

                        published_date = detail.get("published_date", "")
                        last_updated = detail.get("last_updated", "")
                        doc_desc = detail.get("doc_desc", "")
                        doc_type = detail.get("doc_type", "")

                        # Build abstract: document description + section context
                        abstract_parts: list[str] = []
                        if doc_desc and len(doc_desc) >= 20:
                            abstract_parts.append(doc_desc)

                        sec_desc = _strip_xlsx_refs(doc.get("section_desc", ""))
                        # Use first coherent sentence(s) of section description
                        if sec_desc:
                            # Split on sentence boundaries; take up to 300 chars
                            first_sents = re.split(r"(?<=[.!?])\s+", sec_desc)
                            sec_snippet = ""
                            for sent in first_sents:
                                if len(sec_snippet) + len(sent) < 400:
                                    sec_snippet = (sec_snippet + " " + sent).strip()
                                else:
                                    break
                            if sec_snippet and sec_snippet not in " ".join(abstract_parts):
                                abstract_parts.append(sec_snippet)

                        abstract = "\n\n".join(p for p in abstract_parts if p).strip()

                        if len(abstract) < 50:
                            print(
                                f"[{_SITE_ID}] Skipping {doc_id}: abstract too short "
                                f"({len(abstract)} chars) title={title[:50]}"
                            )
                            continue

                        # Pad to >=100 chars if needed (use title + section)
                        if len(abstract) < 100:
                            abstract = f"{title}. {abstract}"

                        # Still short? Add section name
                        if len(abstract) < 100:
                            abstract = (
                                f"{doc['section']}: {abstract}"
                            )

                        pdf_url = f"{_BASE_URL}/dmsdocument/{doc_id}"

                        # Original filename: slug from URL + extension
                        slug_m = re.search(
                            r"/dmsdocument/\d+-([^\"'?#\s/]+)", doc_url
                        )
                        slug = slug_m.group(1) if slug_m else ""
                        original_filename = (
                            f"{slug}.{doc['ext'].lower()}"
                            if slug and doc["ext"]
                            else None
                        )

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": doc_id,
                            "post_number": doc_id,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date or last_updated,
                            "listed_date": last_updated or published_date,
                            "url": doc_url,
                            "pdf_url": pdf_url,
                            "authors": "",
                            "publisher": "Ministry for Primary Industries",
                            "department": "Te Uru Rākau – New Zealand Forest Service",
                            "journal": "",
                            "keywords": (
                                "forestry,wood processing,timber,roundwood,"
                                "New Zealand,MPI,statistics"
                            ),
                            "category": doc["section"],
                            "doi": "",
                            "original_filename": original_filename,
                            "metadata": json.dumps(
                                {
                                    "doc_id": doc_id,
                                    "doc_type": doc_type,
                                    "file_extension": doc["ext"],
                                    "file_size_bytes": doc["size_bytes"],
                                    "section": doc["section"],
                                    "posted_date": last_updated or published_date,
                                    "published_raw": detail.get("published_raw", ""),
                                    "updated_raw": detail.get("updated_raw", ""),
                                    "originalFilename": original_filename,
                                },
                                ensure_ascii=False,
                            ),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(
                            f"[{_SITE_ID}] Saved {saved}/{limit_str}: {title[:70]}"
                        )

                        time.sleep(self._delay)

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{_SITE_ID}] item {doc_id} failed: {exc}")
                        continue

                print(f"[{_SITE_ID}] Done. Total saved: {saved}")
                return saved

            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass
