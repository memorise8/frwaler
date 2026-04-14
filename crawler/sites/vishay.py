# -*- coding: utf-8 -*-
"""Vishay Intertechnology product crawler."""

import json
import os
import time
import uuid

from ..base_crawler import BaseCrawler
from .. import db


class VishayCrawler(BaseCrawler):
    """Crawler for Vishay Intertechnology (Next.js SSG JSON API)."""

    site_id = "vishay"
    site_name = "Vishay Intertechnology"
    base_url = "https://www.vishay.com"

    def __init__(self, db_conn, delay=None):
        super().__init__(db_conn, delay or 1.5)

        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "html_archive", "vishay"
        )
        os.makedirs(self._archive_dir, exist_ok=True)

        db.init_db(db_conn)
        db.register_site(db_conn, self.site_id, self.site_name, self.base_url)

        self._categories = [
            # Diodes
            "diodes/tvs-protection",
            "diodes/esd-protection",
            "diodes/schottky",
            "diodes/ss-schottky",
            "diodes/standard-recovery",
            "diodes/ultrafast-recovery",
            "diodes/switching",
            "diodes/zener-stabilizers",
            "diodes/bridge",
            "diodes/silicon-carbide",
            "diodes/med-high-diodes",
            "diodes/emi-filter",
            # Resistors
            "resistors-fixed",
            "networks-and-arrays",
            "potentiometers",
            "thermistors",
            "trimmers",
            "varistors",
            # Capacitors
            "capacitors/ceramic",
            "capacitors/tantalum",
            "capacitors/film",
            "capacitors/aluminum",
            "capacitors/polymer",
            "capacitors/thin-film",
            "capacitors/surface-mount",
            "capacitors/leaded",
            "capacitors/military-high-reliability",
            "capacitors/automotive",
            "capacitors/high-temperature",
            "capacitors/high-frequency",
            "capacitors/energy-storage",
            "capacitors/power-heavy-current",
            "capacitors/low-esr",
            "capacitors/double-layer",
            # Inductors
            "inductors",
            "inductors/transformers",
            # MOSFETs
            "mosfets",
            "mosfets/automotive-mosfets",
            "mosfets/silicon-carbide-maxsic",
            # Optoelectronics
            "optoelectronics",
            "ir-emitting-diodes",
            "ir-receiver-modules",
            "ir-transceivers",
            "leds",
            "optical-sensors",
            "optocouplers",
            "photo-detectors",
            # Power ICs & Modules
            "power-ics",
            "modules",
            "solid-state-relays",
            "thyristors/phase-control-discrete",
            # Sensors
            "sensors/angular-linear-sensors",
            "sensors/non-contacting-sensors",
            "sensors/sensors-temperature",
        ]

        self._api_base = "https://www.vishay.com/_next/data/vishay-nextjs-ic/en"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None, categories=None):
        """Crawl Vishay product categories and save products to the database."""
        target_categories = categories or self._categories
        saved = 0
        seen_docids = set()

        for category in target_categories:
            if limit is not None and saved >= limit:
                break

            print(f"[{self.site_id}] Fetching category: {category}")
            docids = self._fetch_category(category)
            print(f"[{self.site_id}]   Found {len(docids)} docids in {category}")

            for docid in docids:
                if limit is not None and saved >= limit:
                    break

                if docid in seen_docids:
                    continue
                seen_docids.add(docid)

                # Skip if already in DB
                if db.product_exists(self._conn, self.site_id, external_id=str(docid)):
                    print(f"[{self.site_id}]   Skipping existing docid={docid}")
                    continue

                product = self._fetch_product(docid)
                if product is None:
                    continue

                html_path = self._save_html(docid)
                product["html_path"] = html_path

                db.upsert_product(self._conn, product)
                saved += 1
                print(f"[{self.site_id}]   Saved product {product['name']} (docid={docid}), total={saved}")

                time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Saved {saved} products.")
        return saved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_category(self, category_path):
        """Fetch a category listing JSON and return list of docids."""
        url = f"{self._api_base}/{category_path}.json"
        try:
            resp = self._request(url)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch category: {category_path}")
                return []
            data = resp.json()
            param_results = data.get("pageProps", {}).get("paramResults", [])
            docids = []
            for item in param_results:
                docid = item.get("P1000")
                if docid is not None:
                    docids.append(docid)
            return docids
        except (ValueError, KeyError, Exception) as e:
            print(f"[{self.site_id}] Error fetching category {category_path}: {e}")
            return []

    def _fetch_product(self, docid):
        """Fetch product detail JSON and return a product dict for upsert_product."""
        url = f"{self._api_base}/product/{docid}.json"
        try:
            resp = self._request(url)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch product docid={docid}")
                return None
            data = resp.json()
            page_props = data.get("pageProps", {})

            # Core product info
            cor_results = page_props.get("pCorResults", [])
            cor = cor_results[0] if cor_results else {}

            headline = cor.get("headline", "") or f"docid-{docid}"
            title = cor.get("title", "")
            file_name = cor.get("file_name", "")
            file_ext = cor.get("file_ext", "pdf")
            auto_grade = page_props.get("automotiveGrade", False)

            # Datasheet URL
            datasheet_url = ""
            if file_name:
                datasheet_url = f"https://www.vishay.com/docs/{docid}/{file_name}.{file_ext or 'pdf'}"

            # Image URL
            image_url = f"https://www.vishay.com/images/product-images/pt-large/{docid}-pt-large.jpg"

            # Category breadcrumb
            category_str = self._build_category(page_props)

            # Features → description
            features_results = page_props.get("featuresResults", [])
            features_lines = [f.get("features", "") for f in features_results if f.get("features")]
            description = title
            if features_lines:
                description = "\n".join(features_lines)

            # Specs from parametric data (P-fields on the core result)
            specs = {}
            for key, val in cor.items():
                if key.startswith("P") and key[1:].isdigit() and val not in (None, "", []):
                    specs[key] = val

            product_url = f"https://www.vishay.com/en/product/{docid}/"

            return {
                "id": str(uuid.uuid4()),
                "site_id": self.site_id,
                "external_id": str(docid),
                "name": headline,
                "price": None,
                "price_value": None,
                "currency": None,
                "brand": "Vishay",
                "category": category_str,
                "description": description,
                "image_url": image_url,
                "image_urls": json.dumps([image_url]),
                "specs": json.dumps(specs),
                "rating": None,
                "review_count": None,
                "availability": None,
                "url": product_url,
                "html_path": None,  # filled in by caller
                "metadata": json.dumps({
                    "datasheet_url": datasheet_url,
                    "automotive_grade": auto_grade,
                }),
            }
        except (ValueError, KeyError, IndexError, Exception) as e:
            print(f"[{self.site_id}] Error parsing product docid={docid}: {e}")
            return None

    def _save_html(self, docid):
        """Fetch the HTML product page and save it to the archive directory."""
        page_url = f"https://www.vishay.com/en/product/{docid}/"
        try:
            resp = self._request(page_url)
            if resp is None:
                return None
            html_path = os.path.abspath(os.path.join(self._archive_dir, f"{docid}.html"))
            with open(html_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(resp.text)
            return html_path
        except Exception as e:
            print(f"[{self.site_id}] Failed to save HTML for docid={docid}: {e}")
            return None

    def _build_category(self, page_props):
        """Build a category breadcrumb string from getcatId node chain."""
        try:
            get_cat = page_props.get("getcatId", [])
            if not get_cat:
                return ""
            node = get_cat[0].get("node", {})
            parts = []
            # Walk up the parent chain (max depth guard)
            for _ in range(6):
                name = node.get("categoryName", "")
                if name:
                    parts.append(name)
                parent = node.get("parent")
                if not parent or not isinstance(parent, dict):
                    break
                node = parent
            parts.reverse()
            return " > ".join(parts)
        except Exception:
            return ""
