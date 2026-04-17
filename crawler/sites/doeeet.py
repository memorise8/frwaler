# -*- coding: utf-8 -*-
"""Doeeet space/EEE parts catalogue BJT crawler."""

import json
import time
import uuid

from bs4 import BeautifulSoup

from ..base_crawler import BaseCrawler
from .. import db

# Doeeet (doEEEt by Alter Technology) — space/EEE parts catalogue.
#
# IMPORTANT: The site requires a free account login to access any part search
# or catalogue tool (Comparator, Stockplace, DCL Manager).  There are NO
# publicly accessible category pages for BJTs or MOSFETs.
#
# The entry URLs below are the authenticated tool pages.  Crawling them
# requires a valid session cookie; set DOEEET_SESSION_COOKIE env var
# (copy from a logged-in browser) and pass it via cookies= to __init__.
#
# Verified URL structure (2026-04):
#   ESA Stockplace: https://www.doeeet.com/stockplace          (login required)
#   Comparator:     https://www.doeeet.com/comparator          (login required)
#
# Without auth the crawler will receive a login-redirect (HTTP 200 login page)
# and _parse_listing() will find no products — safe no-op.
ENTRY_URLS = [
    "https://www.doeeet.com/stockplace",
    "https://www.doeeet.com/comparator",
]

_QUAL_KEYWORDS = (
    "JANS", "JANSR", "JANTXV", "JANTX", "JAN",
    "MIL-PRF-19500", "SMD-5962", "ESCC", "COTS-upscreened",
    "space grade", "space-grade", "military",
)


class DoeeetCrawler(BaseCrawler):
    """Crawler for Doeeet.com EEE parts catalogue – space-grade BJTs.

    ENTRY_URLS is intentionally empty.  The crawler is no-op-safe: if the
    list is empty, crawl() returns 0 and logs a TODO.

    To activate:
    1. Confirm the correct category URL(s) on doeeet.com.
    2. Add them to ENTRY_URLS at the top of this file (or subclass and
       override the attribute).
    """

    site_id = "doeeet"
    site_name = "Doeeet"
    base_url = "https://doeeet.com"

    ENTRY_URLS = ENTRY_URLS  # class-level copy; override in tests

    def __init__(self, db_conn, delay=None):
        super().__init__(db_conn, delay or 2.0, respect_robots=True)
        db.init_db(db_conn)
        db.register_site(db_conn, self.site_id, self.site_name, self.base_url)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Doeeet BJT listings and save to database.

        Returns 0 (no-op) if ENTRY_URLS is not configured.
        """
        if not self.ENTRY_URLS:
            print(
                f"[{self.site_id}] TODO: ENTRY_URLS is empty. "
                "Configure the correct Doeeet category URL(s) before crawling."
            )
            return 0

        saved = 0
        for entry_url in self.ENTRY_URLS:
            if limit is not None and saved >= limit:
                break
            page = 1
            while True:
                if limit is not None and saved >= limit:
                    break

                params = {"page": str(page)}
                resp = self._request(entry_url, params=params)
                if resp is None:
                    print(f"[{self.site_id}] Failed to fetch {entry_url} page {page}. Stopping.")
                    break

                soup = BeautifulSoup(resp.text, "html.parser")
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

                # Check for next page link
                next_link = soup.select_one("a[rel='next'], a.next-page, li.next a")
                if not next_link:
                    break
                page += 1
                time.sleep(self._current_delay)

        print(f"[{self.site_id}] Done. Saved {saved} products.")
        return saved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse_listing(self, soup):
        """Extract product entries from a Doeeet listing page."""
        products = []

        # Generic part-listing selectors — adjust once real HTML is known
        selectors = [
            "div.part-item",
            "div.product-item",
            "tr.part-row",
            "li.part-result",
            "div[class*='part-card']",
            "article.part",
        ]

        items = []
        for sel in selectors:
            items = soup.select(sel)
            if items:
                break

        for item in items:
            p = self._parse_item(item)
            if p:
                products.append(p)

        return products

    def _parse_item(self, item):
        """Parse a single listing element into a product dict."""
        try:
            # MPN
            mpn_tag = (item.select_one("[class*='mpn'], [class*='part-number'], "
                                       "[class*='partnum'], [itemprop='mpn']"))
            if not mpn_tag:
                # Fallback: first anchor text that looks like a part number
                for a in item.select("a"):
                    text = a.get_text(strip=True)
                    if text and len(text) >= 4:
                        mpn_tag = a
                        break
            if not mpn_tag:
                return None
            mpn = mpn_tag.get_text(strip=True)
            if not mpn:
                return None

            # Detail URL
            link = item.select_one("a[href]")
            href = link.get("href", "") if link else ""
            detail_url = href if href.startswith("http") else f"{self.base_url}{href}"

            # Manufacturer
            mfr_tag = item.select_one("[class*='manufacturer'], [class*='brand'], [itemprop='brand']")
            brand = mfr_tag.get_text(strip=True) if mfr_tag else ""

            # Description / name
            desc_tag = item.select_one("[class*='description'], [class*='title'], h2, h3")
            name = desc_tag.get_text(strip=True) if desc_tag else mpn

            # Datasheet link
            ds_tag = item.select_one("a[href*='datasheet'], a[href*='.pdf']")
            datasheet_url = ds_tag.get("href", "") if ds_tag else ""

            # Qual level from title + any badge/tag
            qual_tag = item.select_one("[class*='qual'], [class*='grade'], [class*='mil']")
            qual_text = (qual_tag.get_text(strip=True) if qual_tag else "") + " " + name
            qual_level = self._detect_qual_level(qual_text)

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
                "url": detail_url,
                "html_path": None,
                "metadata": json.dumps({
                    "datasheet_url": datasheet_url,
                    "qual_level": qual_level,
                    "is_heritage": True,  # Doeeet is a heritage/EEE catalogue
                }),
            }
        except Exception as exc:
            print(f"[{self.site_id}] Item parse error: {exc}")
            return None

    @staticmethod
    def _detect_qual_level(text):
        """Return the first matching qual keyword found in text, or None."""
        upper = (text or "").upper()
        for kw in _QUAL_KEYWORDS:
            if kw.upper() in upper:
                return kw
        return None
