# -*- coding: utf-8 -*-
"""IRSN / ASNR (FR) press releases — communiqués de presse.

Target: https://www.irsn.fr/actualites/communiques-presse
        (301 → https://recherche-expertise.asnr.fr/actualites/communiques-presse)

Site is Drupal 10.  Listing uses simple ?page=N pagination (16 items/page,
~18 pages = ~288 items total).  Detail pages have no numeric node-id exposed
in the HTML; we use the URL slug as external_id / post_number.

Abstract source priority:
  1. <meta name="description"> (typically 150-300 chars)
  2. All div.irsn-richtext__content sections concatenated
  Combined to guarantee >= 100 chars for any real press release.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _BS4 = True
except ImportError:
    _BS4 = False

_ACTUAL_BASE = "https://recherche-expertise.asnr.fr"
_LIST_PATH = "/actualites/communiques-presse"
_MAX_PAGES = 200
_WALL_CLOCK = 25 * 60          # 25 minutes
_MIN_ABSTRACT = 100            # skip items shorter than this
_BACKOFF = (1, 3, 9)           # retry wait-times (seconds)


class IRSNFrActualitesCrawler(BaseCrawler):
    """Press releases from IRSN (renamed to ASNR in 2025), France."""

    site_id = "irsn-fr-actualites"
    site_name = "Custom: irsn-fr-actualites"
    base_url = "https://www.irsn.fr"

    # ------------------------------------------------------------------ #
    # Low-level HTTP                                                       #
    # ------------------------------------------------------------------ #

    def _curl_get(self, url: str) -> str | None:
        """GET *url* via curl; returns decoded text or None on failure."""
        for attempt in range(3):
            try:
                r = subprocess.run(
                    [
                        "curl", "-sk", "--tls-max", "1.3", "-L",
                        "--max-time", "30",
                        "-H", f"User-Agent: {self.USER_AGENT}",
                        url,
                    ],
                    capture_output=True,
                    timeout=35,
                )
                raw = r.stdout
                if raw and raw.strip():
                    return raw.decode("utf-8", errors="replace")
            except Exception as exc:
                wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                if attempt < 2:
                    print(
                        f"[{self.site_id}] curl attempt {attempt + 1}/3 failed "
                        f"for {url[:80]}: {exc}; retrying in {wait}s"
                    )
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts for {url[:80]}: {exc}")
        return None

    # ------------------------------------------------------------------ #
    # HTML parsing                                                         #
    # ------------------------------------------------------------------ #

    def _soup(self, raw: str):
        """Parse HTML with html5lib → lxml → html.parser fallback chain."""
        if not _BS4:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return _BS(raw, parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------ #
    # Date helper                                                          #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _parse_date(s: str) -> str:
        """Convert DD/MM/YYYY → YYYY-MM-DD; pass through other formats."""
        if not s:
            return ""
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s.strip())
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return s.strip()

    # ------------------------------------------------------------------ #
    # Listing page                                                         #
    # ------------------------------------------------------------------ #

    def _fetch_listing(self, page: int) -> list[dict]:
        """Return list of card dicts from one listing page, or [] on failure."""
        url = f"{_ACTUAL_BASE}{_LIST_PATH}?page={page}"
        raw = self._curl_get(url)
        if not raw:
            return []
        soup = self._soup(raw)
        if not soup:
            return []

        items: list[dict] = []
        for card in soup.select("div.irsn-related-card"):
            link = card.select_one("a.irsn-related-card__link")
            if not link:
                continue
            href = link.get("href", "").strip()
            if not href:
                continue

            title_el = card.select_one("div.irsn-related-card__title")
            title = title_el.get_text(strip=True) if title_el else ""
            if not title:
                continue

            date_el = card.select_one("p.fr-card__date")
            date_str = date_el.get_text(strip=True) if date_el else ""

            tag_el = card.select_one("p.fr-tag")
            category = tag_el.get_text(strip=True) if tag_el else ""

            items.append(
                {
                    "href": href,
                    "title": title,
                    "date_str": date_str,
                    "category": category,
                }
            )
        return items

    # ------------------------------------------------------------------ #
    # Detail page                                                          #
    # ------------------------------------------------------------------ #

    def _fetch_detail(self, url: str) -> dict:
        """Fetch and parse an article detail page.

        Returns a dict with keys: abstract, date_str, pdf_url,
        original_filename, node_id.  Missing keys default to "".
        """
        raw = self._curl_get(url)
        if not raw:
            return {}

        soup = self._soup(raw)
        if not soup:
            return {}

        # 1. Meta description — brief summary (typically 150-300 chars)
        meta = soup.find("meta", attrs={"name": "description"})
        meta_desc = meta.get("content", "").strip() if meta else ""

        # 2. Full richtext body (may span several div.irsn-richtext__content)
        body_parts: list[str] = []
        for div in soup.select("div.irsn-richtext__content"):
            text = div.get_text(" ", strip=True)
            if text:
                body_parts.append(text)
        body_text = " ".join(body_parts)

        # Combine: if meta_desc is already captured in body_text skip duplication
        if meta_desc and body_text:
            # Check if the first 60 chars of meta_desc appear in body_text
            if meta_desc[:60] in body_text:
                abstract = body_text
            else:
                abstract = meta_desc + "\n\n" + body_text
        elif body_text:
            abstract = body_text
        else:
            abstract = meta_desc

        # 3. Published date from article header
        date_el = soup.select_one("div.content-container__date")
        date_str = date_el.get_text(strip=True) if date_el else ""

        # 4. First PDF attachment in /sites/default/files/
        pdf_url: str | None = None
        original_filename: str | None = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/sites/default/files/" in href and ".pdf" in href.lower():
                if not href.startswith("http"):
                    href = _ACTUAL_BASE + href
                pdf_url = href
                fn = href.rstrip("/").split("/")[-1].split("?")[0]
                original_filename = fn if fn else None
                break

        # 5. Drupal node ID (data-history-node-id attribute, if present)
        node_el = soup.find(attrs={"data-history-node-id": True})
        node_id = node_el.get("data-history-node-id") if node_el else None

        return {
            "abstract": abstract,
            "date_str": date_str,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "node_id": node_id,
        }

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()

        for page in range(_MAX_PAGES):
            # --- Safety / budget checks ---
            if limit is not None and saved >= limit:
                break
            elapsed = time.time() - start_time
            if elapsed > _WALL_CLOCK:
                print(
                    f"[{self.site_id}] Wall-clock budget reached at page {page}. Stopping."
                )
                break
            if page == _MAX_PAGES - 1:
                print(
                    f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached. Stopping."
                )
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            # --- Fetch listing ---
            items = self._fetch_listing(page)
            if not items:
                print(f"[{self.site_id}] page {page}: no items. Done.")
                break

            new_items = [i for i in items if i["href"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] page {page}: all items already seen. Stopping.")
                break

            # --- Process each card ---
            for item in new_items:
                if limit is not None and saved >= limit:
                    break

                href = item["href"]
                if href in seen_urls:
                    continue
                seen_urls.add(href)

                detail_url = (
                    href if href.startswith("http") else f"{_ACTUAL_BASE}{href}"
                )

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(detail_url)

                    title = item["title"]
                    abstract = detail.get("abstract", "")

                    if len(abstract) < _MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skipping — abstract "
                            f"({len(abstract)} chars) < {_MIN_ABSTRACT}: {title[:50]}"
                        )
                        continue

                    # Prefer date from detail page body; fall back to listing card
                    date_str = detail.get("date_str") or item["date_str"]
                    published_date = self._parse_date(date_str)
                    listed_date = self._parse_date(item["date_str"])

                    slug = href.rstrip("/").split("/")[-1]

                    self._save_paper(
                        {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": slug,
                            "post_number": slug,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "posted_date": listed_date,
                            "url": detail_url,
                            "pdf_url": detail.get("pdf_url") or None,
                            "original_filename": detail.get("original_filename") or None,
                            "publisher": "IRSN / ASNR",
                            "authors": None,
                            "department": None,
                            "journal": None,
                            "category": item.get("category", ""),
                            "doi": None,
                            "keywords": item.get("category") or None,
                            "metadata": json.dumps(
                                {
                                    "posted_date": item["date_str"],
                                    "node_id": detail.get("node_id"),
                                    "slug": slug,
                                    "category": item.get("category", ""),
                                    "originalFilename": detail.get("original_filename"),
                                },
                                ensure_ascii=False,
                            ),
                        }
                    )
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {href} failed: {exc}; continuing")
                    continue

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
