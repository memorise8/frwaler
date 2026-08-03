# -*- coding: utf-8 -*-
"""HRB (Health Research Board) publications crawler — hrb.ie/publication/

Strategy:
  1. WordPress REST API (/wp-json/wp/v2/publication) for paginated list.
  2. Per-item detail page fetch for PDF URL + metadata cards.
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _make_soup(raw):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _strip_html(html):
    """Strip HTML tags and unescape common entities."""
    if not html:
        return ""
    text = re.sub(r"<[^>]+>", " ", html)
    text = text.replace("&nbsp;", " ")
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", '"')
    text = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), text)
    text = re.sub(r"&[a-zA-Z]{2,6};", "", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class HRBIEPublicationsCrawler(BaseCrawler):
    """Crawler for HRB (Health Research Board) publications."""

    site_id = "hrb-ie-publications"
    site_name = "Custom: hrb-ie-publications"
    base_url = "https://www.hrb.ie"

    _API_BASE = "https://www.hrb.ie/wp-json/wp/v2/publication"
    _PER_PAGE = 10

    # ------------------------------------------------------------------
    # Network helper
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl; returns raw text or None after 3 attempts."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json, text/html, */*",
            url,
        ]
        for attempt in range(3):
            try:
                r = subprocess.run(
                    cmd, capture_output=True, timeout=35,
                    encoding="utf-8", errors="replace",
                )
                if r.stdout.strip():
                    return r.stdout
            except Exception as exc:
                print(f"[hrb-ie-publications] curl error (attempt {attempt+1}/3): {exc}")
            wait = (1, 3, 9)[attempt]
            if attempt < 2:
                print(f"[hrb-ie-publications] Retrying in {wait}s…")
                time.sleep(wait)
        return None

    # ------------------------------------------------------------------
    # Detail-page scraper
    # ------------------------------------------------------------------

    def _parse_detail_page(self, url):
        """Fetch publication detail page; return PDF URL and metadata cards."""
        out = {
            "pdf_url": None,
            "original_filename": None,
            "publisher": None,
            "journal": None,
            "publication_type": None,
            "doi": None,
            "place": None,
        }
        raw = self._curl_get(url)
        if not raw:
            return out

        soup = _make_soup(raw)
        if soup is None:
            return out

        # PDF link inside #downloads-container
        dl_container = soup.find(id="downloads-container")
        if dl_container:
            for a in dl_container.find_all("a", href=True):
                href = a["href"]
                if ".pdf" in href.lower():
                    out["pdf_url"] = href
                    fname = href.rstrip("/").split("/")[-1]
                    if fname:
                        out["original_filename"] = fname
                    break

        # Metadata small-cards
        meta_div = soup.find(
            class_=lambda c: isinstance(c, list) and "publication-meta" in c
                             or isinstance(c, str) and "publication-meta" in c
        )
        if meta_div:
            for card in meta_div.find_all(class_="small-card"):
                title_el = card.find(class_="card-title")
                value_el = card.find(class_="card-value")
                if not title_el or not value_el:
                    continue
                key = title_el.get_text(strip=True).lower()
                a_tag = value_el.find("a", href=True)
                val = a_tag["href"].strip() if a_tag else value_el.get_text(strip=True)
                if "publication type" in key:
                    out["publication_type"] = val
                elif "publisher" in key:
                    out["publisher"] = val
                elif "creator" in key:
                    out["journal"] = val
                elif "doi" in key:
                    out["doi"] = val
                elif "place" in key:
                    out["place"] = val

        return out

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl via WP REST API + per-item detail-page scraping.

        Parameters
        ----------
        limit:
            Max records to save (None = unlimited).
        """
        saved = 0
        page = 1
        seen_urls = set()
        start_time = time.time()
        MAX_PAGES = 200
        MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes wall-clock budget

        while True:
            # --- stop conditions ---
            if limit is not None and saved >= limit:
                break
            if page > MAX_PAGES:
                print(f"[hrb-ie-publications] Safety cap of {MAX_PAGES} pages reached. Stopping.")
                break
            if time.time() - start_time > MAX_SECONDS:
                print(f"[hrb-ie-publications] Time budget exceeded ({MAX_SECONDS}s). Stopping.")
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[hrb-ie-publications] page {page}: saved {saved}/{limit_str}")

            api_url = (
                f"{self._API_BASE}"
                f"?per_page={self._PER_PAGE}&page={page}"
                f"&_fields=id,slug,title,date,link,content"
                f"&orderby=date&order=desc"
            )

            # Fetch list with retry
            raw = None
            for attempt in range(3):
                raw = self._curl_get(api_url)
                if raw:
                    break
                wait = (1, 3, 9)[attempt]
                if attempt < 2:
                    time.sleep(wait)

            if not raw:
                print(f"[hrb-ie-publications] Failed to fetch page {page} after retries. Stopping.")
                break

            try:
                items = json.loads(raw)
            except json.JSONDecodeError as exc:
                print(f"[hrb-ie-publications] JSON error at page {page}: {exc}. Stopping.")
                break

            if not isinstance(items, list) or not items:
                print(f"[hrb-ie-publications] No more items at page {page}. Done.")
                break

            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    item_url = item.get("link", "")

                    # URL deduplication — guards against silent pagination loops
                    if item_url in seen_urls:
                        continue
                    seen_urls.add(item_url)

                    wp_id = item.get("id")
                    title = _strip_html(item.get("title", {}).get("rendered", ""))
                    raw_date = item.get("date", "")
                    published_date = raw_date[:10] if raw_date else None

                    # Abstract comes from REST API content field
                    content_html = item.get("content", {}).get("rendered", "")
                    abstract = _strip_html(content_html)

                    if len(abstract) < 50:
                        print(
                            f"[hrb-ie-publications] Skipping '{title[:50]}' "
                            f"— abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Fetch detail page for PDF + metadata cards
                    time.sleep(self._delay)
                    detail = self._parse_detail_page(item_url)

                    # Extract bare DOI string from DOI URL
                    doi_str = None
                    raw_doi = detail.get("doi") or ""
                    if raw_doi:
                        m = re.search(r"10\.\d{4,}/\S+", raw_doi)
                        if m:
                            doi_str = m.group(0).rstrip(".,)")

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": str(wp_id),
                        "post_number": str(wp_id),
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": published_date,
                        "url": item_url,
                        "pdf_url": detail.get("pdf_url"),
                        "original_filename": detail.get("original_filename"),
                        "publisher": detail.get("publisher"),
                        "journal": detail.get("journal"),
                        "doi": doi_str,
                        "category": detail.get("publication_type"),
                        "authors": None,
                        "keywords": None,
                        "department": None,
                        "metadata": json.dumps({
                            "posted_date": raw_date,
                            "wp_id": wp_id,
                            "slug": item.get("slug"),
                            "publication_type": detail.get("publication_type"),
                            "place_of_publication": detail.get("place"),
                            "creator": detail.get("journal"),
                            "doi_url": raw_doi or None,
                            "originalFilename": detail.get("original_filename"),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[hrb-ie-publications] Saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[hrb-ie-publications] item {item.get('id', '?')} failed: {exc}")
                    continue

            page += 1

        print(f"[hrb-ie-publications] Done. Total saved: {saved}")
        return saved
