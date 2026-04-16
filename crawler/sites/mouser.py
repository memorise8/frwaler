# -*- coding: utf-8 -*-
"""Mouser Electronics BJT crawler (space/military grade)."""

import json
import os
import time
import uuid

from bs4 import BeautifulSoup

from ..base_crawler import BaseCrawler
from .. import db

# Mouser category URL for bipolar transistors
_CATEGORY_URL = (
    "https://www.mouser.com/c/semiconductors/discrete-semiconductors/"
    "transistors/bipolar-transistors-bjt/"
)

# Space/mil-grade filter query params Mouser uses in its faceted search
_QUAL_FILTER_PARAMS = {
    "qualification": "JAN,JANS,JANTX,JANTXV,JANSR,MIL-PRF-19500",
}

# Qual level keywords to detect in product title / description
_QUAL_KEYWORDS = (
    "JANS", "JANSR", "JANTXV", "JANTX", "JAN",
    "MIL-PRF-19500", "SMD-5962", "ESCC", "COTS-upscreened",
    "military", "space grade", "space-grade",
)


class MouserCrawler(BaseCrawler):
    """Crawler for Mouser Electronics – space/military-grade BJTs.

    Mouser is heavily JavaScript-rendered.  We attempt a plain HTTP request
    first; if the response looks like an empty SPA shell we log a warning and
    return 0 (no-op safe).

    Optional REST API path: set MOUSER_API_KEY env var to enable the Mouser
    Search API (POST /api/v1/search/keyword).  Without the key we fall back to
    HTML scraping.
    """

    site_id = "mouser"
    site_name = "Mouser"
    base_url = "https://www.mouser.com"

    # Mouser category entry URL for bipolar transistors
    ENTRY_URLS = [_CATEGORY_URL]

    def __init__(self, db_conn, delay=None):
        super().__init__(db_conn, delay or 2.0, respect_robots=True)
        self._api_key = os.environ.get("MOUSER_API_KEY", "")
        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "html_archive", "mouser"
        )
        os.makedirs(self._archive_dir, exist_ok=True)
        db.init_db(db_conn)
        db.register_site(db_conn, self.site_id, self.site_name, self.base_url)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Mouser BJT category and save products to the database."""
        if self._api_key:
            return self._crawl_via_api(limit)
        return self._crawl_via_html(limit)

    # ------------------------------------------------------------------
    # HTML path
    # ------------------------------------------------------------------

    def _crawl_via_html(self, limit=None):
        saved = 0
        for entry_url in self.ENTRY_URLS:
            if limit is not None and saved >= limit:
                break
            page = 1
            while True:
                if limit is not None and saved >= limit:
                    break
                params = {"Ns": "Pricing|0", "FS": "True", "PageSize": "50",
                          "PageNumber": str(page)}
                resp = self._request(entry_url, params=params)
                if resp is None:
                    print(f"[{self.site_id}] Failed to fetch page {page}. Stopping.")
                    break

                soup = BeautifulSoup(resp.text, "html.parser")

                # Detect SPA shell (empty content)
                if not soup.select("div.product-list, tr.search-result, "
                                   "div[data-testid='product-list'], table.SearchResultsTable"):
                    print(
                        f"[{self.site_id}] Page appears to be JS-rendered (SPA). "
                        "Set MOUSER_API_KEY to use the REST API instead. "
                        "Stopping HTML crawl."
                    )
                    break

                products = self._parse_listing(soup)
                if not products:
                    print(f"[{self.site_id}] No products on page {page}. Done.")
                    break

                print(f"[{self.site_id}] Page {page}: {len(products)} products")
                for p in products:
                    if limit is not None and saved >= limit:
                        break
                    if db.product_exists(self._conn, self.site_id,
                                         url=p.get("url"),
                                         external_id=p.get("external_id")):
                        continue
                    db.upsert_product(self._conn, p)
                    saved += 1

                page += 1
                time.sleep(self._current_delay)

        print(f"[{self.site_id}] Done. Saved {saved} products.")
        return saved

    def _parse_listing(self, soup):
        """Extract product rows from a Mouser category listing page."""
        products = []

        # Try table-based layout (classic Mouser)
        for row in soup.select("tr.search-result, tr[class*='SearchResultsRow']"):
            p = self._parse_table_row(row)
            if p:
                products.append(p)

        # Try card/grid layout
        if not products:
            for card in soup.select("div.product-list__item, div[data-partnum]"):
                p = self._parse_card(card)
                if p:
                    products.append(p)

        return products

    def _parse_table_row(self, row):
        """Parse a table row into a product dict."""
        try:
            # MPN
            mpn_tag = (row.select_one("a.mfr-part-num")
                       or row.select_one("td.mfr-part-num a")
                       or row.select_one("span[id*='MPN']"))
            if not mpn_tag:
                return None
            mpn = mpn_tag.get_text(strip=True)
            if not mpn:
                return None

            detail_href = mpn_tag.get("href", "")
            detail_url = (detail_href if detail_href.startswith("http")
                          else f"{self.base_url}{detail_href}")

            # Manufacturer
            mfr_tag = row.select_one("a.manufacturer-name, td.manufacturer a")
            brand = mfr_tag.get_text(strip=True) if mfr_tag else ""

            # Description / name
            desc_tag = row.select_one("td.description, span.description")
            name = desc_tag.get_text(strip=True) if desc_tag else mpn

            # Datasheet
            ds_tag = row.select_one("a[href*='datasheet'], a[title*='Datasheet']")
            datasheet_url = ds_tag.get("href", "") if ds_tag else ""

            qual_level = self._detect_qual_level(name)

            return self._build_product(mpn, name, brand, detail_url, datasheet_url, qual_level)
        except Exception as exc:
            print(f"[{self.site_id}] Row parse error: {exc}")
            return None

    def _parse_card(self, card):
        """Parse a card/grid element into a product dict."""
        try:
            mpn = card.get("data-partnum") or ""
            if not mpn:
                mpn_tag = card.select_one("[class*='part-number'], [class*='partnum']")
                mpn = mpn_tag.get_text(strip=True) if mpn_tag else ""
            if not mpn:
                return None

            detail_tag = card.select_one("a[href*='/ProductDetail/']")
            detail_href = detail_tag.get("href", "") if detail_tag else ""
            detail_url = (detail_href if detail_href.startswith("http")
                          else f"{self.base_url}{detail_href}")

            name_tag = card.select_one("[class*='description'], [class*='product-name']")
            name = name_tag.get_text(strip=True) if name_tag else mpn

            brand_tag = card.select_one("[class*='manufacturer']")
            brand = brand_tag.get_text(strip=True) if brand_tag else ""

            ds_tag = card.select_one("a[href*='datasheet']")
            datasheet_url = ds_tag.get("href", "") if ds_tag else ""

            qual_level = self._detect_qual_level(name)

            return self._build_product(mpn, name, brand, detail_url, datasheet_url, qual_level)
        except Exception as exc:
            print(f"[{self.site_id}] Card parse error: {exc}")
            return None

    # ------------------------------------------------------------------
    # REST API path (MOUSER_API_KEY required)
    # ------------------------------------------------------------------

    def _crawl_via_api(self, limit=None):
        """Use Mouser Search API to find space/mil BJTs.

        TODO: Implement full pagination via the API's Records/RecordCount
              fields once entry category codes are confirmed.
        """
        # TODO: resolve Mouser category ID for BJTs via
        #   GET https://api.mouser.com/api/v1/category?apiKey=<key>
        # and iterate pages with {"SearchByKeywordRequest": {"keyword": "BJT JAN",
        #   "records": 50, "startingRecord": <offset>, ...}}
        print(
            f"[{self.site_id}] MOUSER_API_KEY detected. "
            "REST API crawl is stubbed — implement _crawl_via_api() with "
            "POST https://api.mouser.com/api/v1/search/keyword?apiKey=<key>"
        )
        return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_product(self, mpn, name, brand, url, datasheet_url, qual_level):
        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": mpn,
            "name": name,
            "price": None,
            "price_value": None,
            "currency": None,
            "brand": brand,
            "category": "BJT",
            "description": name,
            "image_url": None,
            "image_urls": None,
            "specs": None,
            "rating": None,
            "review_count": None,
            "availability": None,
            "url": url,
            "html_path": None,
            "metadata": json.dumps({
                "datasheet_url": datasheet_url,
                "qual_level": qual_level,
                "is_heritage": bool(qual_level),
            }),
        }

    @staticmethod
    def _detect_qual_level(text):
        """Return the first matching qual keyword found in text, or None."""
        upper = (text or "").upper()
        for kw in _QUAL_KEYWORDS:
            if kw.upper() in upper:
                return kw
        return None
