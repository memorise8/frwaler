# -*- coding: utf-8 -*-
"""Nofima publication crawler — Nofima's reports (bc_tax_pubtype/nofimareports).

List URL:  https://nofima.com/bc_tax_pubtype/nofimareports/page/{N}/
Detail:    https://nofima.com/publication/{cristin_id}/
API:       WordPress HTML archive (bc_publication post type, not REST-exposed)
"""

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Absolute import — spec_from_file_location has no package context.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from crawler.base_crawler import BaseCrawler  # noqa: E402

# ---------------------------------------------------------------------------
# Parser helpers
# ---------------------------------------------------------------------------

_BS_PARSERS = ("html5lib", "lxml", "html.parser")


def _make_soup(raw):
    """Try BS4 parsers in order; return a BeautifulSoup object or None."""
    from bs4 import BeautifulSoup

    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            raw = raw.decode("utf-8", errors="replace")
    for parser in _BS_PARSERS:
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

_SITE_ID = "nofima-com-publication"


def _curl_get(url, retries=3, timeout=30):
    """GET via curl with exponential backoff; returns text or None."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", str(timeout),
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: en-US,en;q=0.9",
        "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        url,
    ]
    waits = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                time.sleep(waits[attempt])
        except Exception as exc:
            if attempt < retries - 1:
                print(f"[{_SITE_ID}] curl error {url}: {exc}, retrying in {waits[attempt]}s")
                time.sleep(waits[attempt])
            else:
                print(f"[{_SITE_ID}] curl failed after {retries} attempts: {url}: {exc}")
    return None


# ---------------------------------------------------------------------------
# Date / text helpers
# ---------------------------------------------------------------------------

def _parse_date(raw):
    """Parse 'DD.MM.YYYY' or 'YYYY' into ISO format; return raw string on failure."""
    if not raw:
        return None
    raw = raw.strip()
    m = re.match(r'^(\d{1,2})\.(\d{1,2})\.(\d{4})$', raw)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    if re.match(r'^\d{4}$', raw):
        return raw
    return raw or None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class NofimaCOMPublicationCrawler(BaseCrawler):
    """Crawls Nofima's reports from nofima.com."""

    site_id = "nofima-com-publication"
    site_name = "Custom: nofima-com-publication"
    base_url = "https://nofima.com"

    _LIST_BASE = "https://nofima.com/bc_tax_pubtype/nofimareports/"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_MINUTES = 25

    # ------------------------------------------------------------------
    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        page = 1
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            if limit is not None and saved >= limit:
                break
            if page > self._MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping")
                break
            elapsed_min = (time.time() - start_time) / 60
            if elapsed_min > self._MAX_MINUTES:
                print(f"[{self.site_id}] Time budget ({self._MAX_MINUTES}min) exceeded, exiting cleanly")
                break

            list_url = self._LIST_BASE if page == 1 else f"{self._LIST_BASE}page/{page}/"

            raw = _curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] Failed to fetch list page {page}, stopping")
                break

            soup = _make_soup(raw)
            if soup is None:
                print(f"[{self.site_id}] Failed to parse list page {page}, stopping")
                break

            # Extract publication links from cards
            pub_links = []
            for a in soup.find_all("a", class_="card-publication"):
                href = (a.get("href") or "").strip()
                if re.search(r"/publication/\d+/", href) and href not in seen_urls:
                    pub_links.append(href)

            if not pub_links:
                print(f"[{self.site_id}] No new publication links on page {page}, stopping")
                break

            for detail_url in pub_links:
                if limit is not None and saved >= limit:
                    break

                seen_urls.add(detail_url)

                try:
                    paper = self._fetch_detail(detail_url)
                    if paper is None:
                        continue
                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(f"[{self.site_id}] Skipping {detail_url}: abstract too short ({len(abstract)} chars)")
                        continue
                    self._save_paper(paper)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            page += 1

        print(f"[{self.site_id}] crawl complete: saved {saved} items across {page - 1} pages")
        return saved

    # ------------------------------------------------------------------
    def _fetch_detail(self, url):
        """Fetch and parse one publication detail page; return paper dict or None."""
        raw = _curl_get(url)
        if not raw:
            print(f"[{self.site_id}] Failed to fetch detail: {url}")
            return None

        soup = _make_soup(raw)
        if soup is None:
            return None

        # External/Cristin ID from URL
        m = re.search(r"/publication/(\d+)/", url)
        if not m:
            return None
        external_id = m.group(1)

        # WP post ID and publication type slug from article element
        article = soup.find("article", id=f"entry-{external_id}")
        wp_post_id = None
        pub_type_slug = None
        if article:
            cls_str = " ".join(article.get("class", []))
            pm = re.search(r"\bpost-(\d+)\b", cls_str)
            if pm:
                wp_post_id = pm.group(1)
            tm = re.search(r"\bbc_tax_pubtype-(\S+)", cls_str)
            if tm:
                pub_type_slug = tm.group(1)

        # Title
        h1 = soup.find("h1", class_="page__title")
        title = h1.get_text(strip=True) if h1 else ""
        if not title:
            return None

        # Authors — the paragraph immediately after h1 in the header with "alignwide"
        authors_str = ""
        header = soup.find("header", class_="page__header")
        if header:
            for p in header.find_all("p"):
                cls_list = p.get("class") or []
                if "alignwide" in cls_list:
                    authors_str = p.get_text(separator="", strip=True)
                    break
        if not authors_str:
            # fallback: find by style attribute pattern
            for p in soup.find_all("p"):
                cls_list = p.get("class") or []
                if "alignwide" in cls_list and "has-sizing-large" in cls_list:
                    authors_str = p.get_text(separator="", strip=True)
                    break
        # Normalize: strip trailing/leading semicolons/spaces
        authors_str = re.sub(r"\s+", " ", authors_str).strip().strip(";").strip()

        # Published date from .page__meta
        published_date = None
        meta_div = soup.find("div", class_="page__meta")
        if meta_div:
            for p in meta_div.find_all("p"):
                text = p.get_text(" ", strip=True)
                dm = re.search(r"Published\s+(\S+)", text, re.IGNORECASE)
                if dm:
                    published_date = _parse_date(dm.group(1))
                    break

        # Abstract — find <h2>Summary</h2> then collect <p> siblings in same parent
        abstract_parts = []
        summary_h2 = None
        for h2 in soup.find_all("h2"):
            if h2.get_text(strip=True).lower() == "summary":
                summary_h2 = h2
                break
        if summary_h2:
            parent_div = summary_h2.parent
            past_h2 = False
            for child in parent_div.children:
                if child is summary_h2:
                    past_h2 = True
                    continue
                if not past_h2:
                    continue
                if not hasattr(child, "name") or child.name is None:
                    continue
                if child.name == "p":
                    txt = child.get_text(" ", strip=True)
                    if txt:
                        abstract_parts.append(txt)
                elif child.name in ("h2", "h3", "h4"):
                    break
        abstract = " ".join(abstract_parts).strip()

        # pub-links section: DOI, NVA handle, direct PDF
        doi = None
        nva_url = None
        pdf_url = None
        for pub_p in soup.find_all("p", class_="pub-links"):
            for a in pub_p.find_all("a", class_="pub-links__item"):
                href = (a.get("href") or "").strip()
                if not href:
                    continue
                if "doi.org" in href:
                    m2 = re.match(r"https?://doi\.org/(.*)", href)
                    doi = m2.group(1) if m2 else href
                elif "hdl.handle.net" in href or "handle.net" in href:
                    nva_url = href
                elif href.lower().endswith(".pdf"):
                    pdf_url = href

        # If no direct PDF, check any other .pdf links on the page
        if not pdf_url:
            for a in soup.find_all("a", href=re.compile(r"\.pdf($|\?)", re.IGNORECASE)):
                pdf_url = a.get("href", "").strip()
                break

        # Publication details section
        journal = None
        publisher = None
        pub_type_label = None
        num_pages = None
        pub_summary = soup.find("div", class_="page__pub-summary")
        if pub_summary:
            for p in pub_summary.find_all("p"):
                strong = p.find("strong")
                if not strong:
                    continue
                label = strong.get_text(strip=True).rstrip(":").strip()
                # Value = full text minus the label
                full_text = p.get_text(" ", strip=True)
                # Remove leading label+colon
                colon_idx = full_text.find(":")
                value = full_text[colon_idx + 1:].strip() if colon_idx >= 0 else full_text.replace(label, "").strip()

                lc = label.lower()
                if lc == "journal":
                    # Strip trailing year like ", 2026" or "  2026"
                    journal = re.sub(r",?\s*\d{4}\s*$", "", value).strip()
                elif lc == "publisher":
                    publisher = value
                elif lc == "publication type":
                    pub_type_label = value
                elif lc in ("number of pages", "pages"):
                    num_pages = value

        # original_filename from pdf_url path segment
        original_filename = None
        if pdf_url:
            m3 = re.search(r"/([^/?#]+\.pdf)(?:[?#]|$)", pdf_url, re.IGNORECASE)
            if m3:
                original_filename = m3.group(1)

        metadata_dict = {
            k: v for k, v in {
                "nva_url": nva_url,
                "cristin_id": external_id,
                "wp_post_id": wp_post_id,
                "pub_type_slug": pub_type_slug,
                "pub_type_label": pub_type_label,
                "num_pages": num_pages,
                "journal_raw": journal,
                "doi": doi,
            }.items() if v is not None
        }

        return {
            "id": f"{self.site_id}:{external_id}",
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": external_id,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "authors": authors_str,
            "publisher": publisher or "Nofima",
            "journal": journal,
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "keywords": None,
            "category": pub_type_label or pub_type_slug or "Nofima's reports",
            "original_filename": original_filename,
            "metadata": json.dumps(metadata_dict, ensure_ascii=False),
        }
