# -*- coding: utf-8 -*-
"""Nexperia product crawler."""

import json
import os
import time
import uuid

from bs4 import BeautifulSoup

from ..base_crawler import BaseCrawler
from .. import db


class NexperiaCrawler(BaseCrawler):
    """Crawler for Nexperia (REST JSON API + server-rendered category pages)."""

    site_id = "nexperia"
    site_name = "Nexperia"
    base_url = "https://www.nexperia.com"

    def __init__(self, db_conn, delay=None):
        super().__init__(db_conn, delay or 2.0, respect_robots=False)

        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "html_archive", "nexperia"
        )
        os.makedirs(self._archive_dir, exist_ok=True)

        db.init_db(db_conn)
        db.register_site(db_conn, self.site_id, self.site_name, self.base_url)

        self._categories = [
            "mosfets/small-signal-mosfets",
            "mosfets/power-mosfets",
            "mosfets/automotive-mosfets",
            "diodes/schottky-diodes-and-rectifiers",
            "diodes/zener-diodes",
            "diodes/switching-diodes",
            "esd-protection-tvs-filtering-and-signal-conditioning/esd-protection",
            "esd-protection-tvs-filtering-and-signal-conditioning/tvs-diodes",
            "bipolar-transistors/general-purpose-bipolar-transistors",
            "bipolar-transistors/low-vcesat-bipolar-transistors",
            "analog-logic-ics/logic-ics",
            "gan-fets/gan-fets",
        ]

        self._api_base = "https://product-data.nexperia.com/.rest/products/v1"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None, categories=None):
        """Crawl Nexperia product categories and save products to the database."""
        target_categories = categories or self._categories
        saved = 0
        seen_parts = set()

        for category in target_categories:
            if limit is not None and saved >= limit:
                break

            print(f"[{self.site_id}] Fetching category: {category}")
            parts = self._fetch_category_parts(category)
            print(f"[{self.site_id}]   Found {len(parts)} parts in {category}")

            for part_number in parts:
                if limit is not None and saved >= limit:
                    break

                if part_number in seen_parts:
                    continue
                seen_parts.add(part_number)

                # Skip if already in DB
                if db.product_exists(self._conn, self.site_id, external_id=part_number):
                    print(f"[{self.site_id}]   Skipping existing part={part_number}")
                    continue

                product = self._fetch_product(part_number)
                if product is None:
                    continue

                html_path = self._save_html(part_number)
                product["html_path"] = html_path

                db.upsert_product(self._conn, product)
                saved += 1
                print(f"[{self.site_id}]   Saved product {part_number}, total={saved}")

                time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Saved {saved} products.")
        return saved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_category_parts(self, category_path):
        """Fetch category HTML and extract part numbers from product links."""
        url = f"{self.base_url}/products/{category_path}/"
        try:
            resp = self._request(url)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch category: {category_path}")
                return []
            soup = BeautifulSoup(resp.text, "html.parser")
            parts = []
            seen = set()
            for a in soup.select('a[href^="/product/"]'):
                href = a.get("href", "")
                # href is like /product/PART_NUMBER or /product/PART_NUMBER/
                part = href.split("/product/", 1)[-1].strip("/").split("/")[0].split("#")[0]
                if part and part not in seen:
                    seen.add(part)
                    parts.append(part)
            return parts
        except Exception as e:
            print(f"[{self.site_id}] Error fetching category {category_path}: {e}")
            return []

    def _fetch_product(self, part_number):
        """Fetch product data from the Nexperia API and return a product dict."""
        api_headers = {
            "Origin": "https://www.nexperia.com",
            "Referer": f"https://www.nexperia.com/product/{part_number}",
            "User-Agent": self.USER_AGENT,
            "Accept": "application/json",
        }

        # --- Product header ---
        header_data = {}
        try:
            header_url = f"{self._api_base}/productHeader?partNumber={part_number}"
            resp = self._request(header_url, headers=api_headers)
            if resp is not None:
                header_data = resp.json()
        except Exception as e:
            print(f"[{self.site_id}] Error fetching header for {part_number}: {e}")

        # --- Product parametrics ---
        specs = {}
        try:
            param_url = f"{self._api_base}/productParametrics?partNumber={part_number}"
            resp = self._request(param_url, headers=api_headers)
            if resp is not None:
                param_data = resp.json()
                specs = self._parse_parametrics(param_data)
        except Exception as e:
            print(f"[{self.site_id}] Error fetching parametrics for {part_number}: {e}")

        # --- HTML page for description / category meta ---
        description = ""
        category_str = ""
        image_url = None
        try:
            page_url = f"{self.base_url}/product/{part_number}"
            resp = self._request(page_url)
            if resp is not None:
                soup = BeautifulSoup(resp.text, "html.parser")
                description, category_str, image_url = self._parse_product_html(soup, part_number)
        except Exception as e:
            print(f"[{self.site_id}] Error fetching product HTML for {part_number}: {e}")

        # Fall back to header data for description if HTML parse failed
        if not description:
            description = (
                header_data.get("description", "")
                or header_data.get("shortDescription", "")
                or ""
            )

        # Availability from pipType
        pip_type = header_data.get("pipType", "") or ""
        availability = pip_type if pip_type else None

        # Datasheet URL
        datasheet_url = f"https://assets.nexperia.com/documents/data-sheet/{part_number}.pdf"

        # Image fallback: use package outline 3D if HTML image not found
        if not image_url:
            package = header_data.get("package", "") or ""
            if package:
                image_url = f"https://assets.nexperia.com/documents/outline-3d/{package}_3d.png"

        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": part_number,
            "name": part_number,
            "price": None,
            "price_value": None,
            "currency": None,
            "brand": "Nexperia",
            "category": category_str,
            "description": description,
            "image_url": image_url,
            "image_urls": json.dumps([image_url]) if image_url else None,
            "specs": json.dumps(specs),
            "rating": None,
            "review_count": None,
            "availability": availability,
            "url": f"{self.base_url}/product/{part_number}",
            "html_path": None,  # filled in by caller
            "metadata": json.dumps({"datasheet_url": datasheet_url}),
        }

    def _parse_parametrics(self, data):
        """Parse parametrics API response (columns + rows) into a flat dict."""
        specs = {}
        try:
            columns = data.get("columns", [])
            rows = data.get("rows", [])
            if not columns or not rows:
                return specs
            # columns is a list of dicts with at least a 'name' key
            col_names = [c.get("name", f"col{i}") for i, c in enumerate(columns)]
            for row in rows:
                values = row if isinstance(row, list) else row.get("values", [])
                for i, val in enumerate(values):
                    if i < len(col_names) and val not in (None, "", []):
                        col = col_names[i]
                        if col not in specs:
                            specs[col] = val
                        elif isinstance(specs[col], list):
                            specs[col].append(val)
                        else:
                            specs[col] = [specs[col], val]
        except Exception as e:
            print(f"[{self.site_id}] Error parsing parametrics: {e}")
        return specs

    def _parse_product_html(self, soup, part_number):
        """Extract description, category, and image from product detail HTML."""
        description = ""
        category_str = ""
        image_url = None

        # Description from visible paragraph
        desc_tag = soup.select_one("p.product-detail-hero__description")
        if desc_tag:
            description = desc_tag.get_text(strip=True)

        # Try Schema.org JSON-LD for description/image fallback
        if not description:
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(script.string or "")
                    if isinstance(ld, dict):
                        description = description or ld.get("description", "")
                        if not image_url:
                            img = ld.get("image", "")
                            image_url = img if isinstance(img, str) else None
                except Exception:
                    pass

        # Category from meta tags
        cat1_tag = soup.find("meta", attrs={"name": "productCategory_level1"})
        cat2_tag = soup.find("meta", attrs={"name": "productCategory_level2"})
        cat1 = cat1_tag["content"].strip() if cat1_tag and cat1_tag.get("content") else ""
        cat2 = cat2_tag["content"].strip() if cat2_tag and cat2_tag.get("content") else ""
        if cat1 and cat2:
            category_str = f"{cat1} > {cat2}"
        elif cat1:
            category_str = cat1

        # Image from hero section
        if not image_url:
            img_tag = soup.select_one("div.product-detail-hero__image img")
            if img_tag:
                src = img_tag.get("src", "")
                if src:
                    image_url = src if src.startswith("http") else f"{self.base_url}{src}"

        return description, category_str, image_url

    def _save_html(self, part_number):
        """Fetch the HTML product page and save it to the archive directory."""
        page_url = f"{self.base_url}/product/{part_number}"
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
            print(f"[{self.site_id}] Failed to save HTML for {part_number}: {e}")
            return None
