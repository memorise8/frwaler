# -*- coding: utf-8 -*-
"""Texas Instruments product crawler."""

import json
import os
import re
import time
import uuid
import xml.etree.ElementTree as ET

from ..base_crawler import BaseCrawler
from .. import db


class TICrawler(BaseCrawler):
    """Crawler for Texas Instruments (sitemap + JSON API)."""

    site_id = "ti"
    site_name = "Texas Instruments"
    base_url = "https://www.ti.com"

    _SITEMAP_URL = "https://www.ti.com/product/sitemap-standard.xml"
    _API_BASE = "https://www.ti.com/productmodel"
    _NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"

    def __init__(self, db_conn, delay=None):
        super().__init__(db_conn, delay or 1.5, respect_robots=False)

        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "html_archive", "ti"
        )
        os.makedirs(self._archive_dir, exist_ok=True)

        db.init_db(db_conn)
        db.register_site(db_conn, self.site_id, self.site_name, self.base_url)

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Fetch sitemap, iterate products, save to DB."""
        print(f"[{self.site_id}] Fetching sitemap: {self._SITEMAP_URL}")
        part_numbers = self._fetch_sitemap()
        print(f"[{self.site_id}] Found {len(part_numbers)} part numbers in sitemap")

        saved = 0
        for part in part_numbers:
            if limit is not None and saved >= limit:
                break

            if db.product_exists(self._conn, self.site_id, external_id=part):
                print(f"[{self.site_id}]   Skipping existing part={part}")
                continue

            product = self._fetch_product(part)
            if product is None:
                continue

            html_path = self._save_html(part)
            product["html_path"] = html_path

            db.upsert_product(self._conn, product)
            saved += 1
            print(f"[{self.site_id}]   Saved {part}, total={saved}")

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Saved {saved} products.")
        return saved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_sitemap(self):
        """Fetch and parse the TI product sitemap, return list of part numbers."""
        try:
            resp = self._request(self._SITEMAP_URL)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch sitemap")
                return []

            root = ET.fromstring(resp.content)
            part_numbers = []
            # Try with and without namespace
            urls = root.findall(f"{self._NS}url")
            if not urls:
                urls = root.findall("url")
            for url_elem in urls:
                loc = url_elem.findtext(f"{self._NS}loc", "") or url_elem.findtext("loc", "")
                # Extract part number from /product/{PART}
                m = re.search(r"/product/([^/?#]+)$", loc)
                if m:
                    part_numbers.append(m.group(1))
            return part_numbers
        except Exception as e:
            print(f"[{self.site_id}] Error parsing sitemap: {e}")
            return []

    def _fetch_product(self, part_number):
        """Fetch product JSON from TI API and return a product dict."""
        url = f"{self._API_BASE}/{part_number}"
        try:
            resp = self._request(url)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch product part={part_number}")
                return None

            data = resp.json()

            # -- Name / description --
            name = data.get("genericPartNumber") or part_number
            description = data.get("deviceDescription") or ""
            long_desc = data.get("deviceLongDescription") or ""
            features_html = data.get("deviceFeatures") or ""

            # Strip basic HTML tags for plain-text description
            def strip_html(text):
                return re.sub(r"<[^>]+>", " ", text).strip()

            if long_desc:
                description = strip_html(long_desc)
            elif description:
                description = strip_html(description)
            elif features_html:
                description = strip_html(features_html)

            # -- Family --
            family = data.get("familyName") or ""

            # -- Availability --
            status = data.get("marketingStatusDescription") or None

            # -- Category from breadcrumb --
            breadcrumbs = data.get("primaryBreadcrumbList") or []
            category_parts = [b.get("name", "") for b in breadcrumbs if b.get("name")]
            category_str = " > ".join(category_parts) if category_parts else ""

            # -- Specs from parametersList --
            specs = {}
            for param in data.get("parametersList") or []:
                title = param.get("title") or param.get("id") or ""
                value = param.get("value")
                if title and value is not None:
                    specs[title] = value

            # -- Image URL --
            image_url = None
            gallery = data.get("imageGallery") or []
            if gallery:
                first = gallery[0]
                image_url = first.get("url") or first.get("imageSrc") or None

            # -- Datasheet URL --
            datasheet_url = None
            for doc_key in ("technicalDocuments", "techDocs"):
                docs = data.get(doc_key) or []
                if isinstance(docs, list):
                    for doc in docs:
                        doc_type = (doc.get("docType") or doc.get("type") or "").lower()
                        doc_url = doc.get("docUrl") or doc.get("url") or doc.get("pdfUrl") or ""
                        if "datasheet" in doc_type or doc_url.lower().endswith(".pdf"):
                            datasheet_url = doc_url
                            break
                if datasheet_url:
                    break

            return {
                "id": str(uuid.uuid4()),
                "site_id": self.site_id,
                "external_id": part_number,
                "name": name,
                "price": None,
                "price_value": None,
                "currency": None,
                "brand": "Texas Instruments",
                "category": category_str,
                "description": description,
                "image_url": image_url,
                "image_urls": json.dumps([image_url]) if image_url else None,
                "specs": json.dumps(specs),
                "rating": None,
                "review_count": None,
                "availability": status,
                "url": f"https://www.ti.com/product/{part_number}",
                "html_path": None,  # filled by caller
                "metadata": json.dumps({
                    "datasheet_url": datasheet_url,
                    "family": family,
                }),
            }
        except (ValueError, KeyError, IndexError, Exception) as e:
            print(f"[{self.site_id}] Error parsing product part={part_number}: {e}")
            return None

    def _save_html(self, part_number):
        """Fetch the HTML product page and save to archive directory."""
        page_url = f"https://www.ti.com/product/{part_number}"
        try:
            resp = self._request(page_url)
            if resp is None:
                return None
            html_path = os.path.abspath(
                os.path.join(self._archive_dir, f"{part_number}.html")
            )
            with open(html_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(resp.text)
            return html_path
        except Exception as e:
            print(f"[{self.site_id}] Failed to save HTML for part={part_number}: {e}")
            return None
