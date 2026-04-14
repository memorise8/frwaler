# -*- coding: utf-8 -*-
"""Infineon Technologies product crawler."""

import json
import os
import time
import uuid
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup

from ..base_crawler import BaseCrawler
from .. import db


class InfineonCrawler(BaseCrawler):
    """Crawler for Infineon Technologies (sitemap XML + HTML parse + dataApi)."""

    site_id = "infineon"
    site_name = "Infineon Technologies"
    base_url = "https://www.infineon.com"

    _SITEMAP_URL = "https://www.infineon.com/en.sitemap.part-row-sitemap.xml"
    _SITEMAP_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
    _CHROME_UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    def __init__(self, db_conn, delay=None):
        super().__init__(db_conn, delay or 1.5, respect_robots=False)

        self._archive_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "html_archive", "infineon"
        )
        os.makedirs(self._archive_dir, exist_ok=True)

        db.init_db(db_conn)
        db.register_site(db_conn, self.site_id, self.site_name, self.base_url)

        # Ensure Chrome User-Agent is set for all requests
        self._session.headers.update({"User-Agent": self._CHROME_UA})

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl Infineon parts from sitemap and save products to the database."""
        print(f"[{self.site_id}] Fetching sitemap: {self._SITEMAP_URL}")
        opns = self._fetch_sitemap()
        print(f"[{self.site_id}] Found {len(opns)} OPNs in sitemap")

        saved = 0
        for opn in opns:
            if limit is not None and saved >= limit:
                break

            if db.product_exists(self._conn, self.site_id, external_id=opn):
                print(f"[{self.site_id}]   Skipping existing OPN={opn}")
                continue

            result = self._fetch_product(opn)
            if result is None:
                continue

            product, html_content = result
            html_path = self._save_html(opn, html_content)
            product["html_path"] = html_path

            db.upsert_product(self._conn, product)
            saved += 1
            print(f"[{self.site_id}]   Saved {product['name']} (OPN={opn}), total={saved}")

            time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Saved {saved} products.")
        return saved

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_sitemap(self):
        """Fetch sitemap XML and return list of OPNs."""
        try:
            resp = self._request(self._SITEMAP_URL)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch sitemap")
                return []

            root = ET.fromstring(resp.content)
            opns = []
            for url_elem in root.findall(f"{self._SITEMAP_NS}url"):
                loc = url_elem.find(f"{self._SITEMAP_NS}loc")
                if loc is None or loc.text is None:
                    continue
                url = loc.text.strip()
                # Extract OPN from /part/{OPN}
                if "/part/" in url:
                    opn = url.split("/part/")[-1].rstrip("/")
                    if opn:
                        opns.append(opn)
            return opns
        except Exception as e:
            print(f"[{self.site_id}] Error fetching sitemap: {e}")
            return []

    def _fetch_product(self, opn):
        """Fetch product HTML + breadcrumb JSON and return (product_dict, html_content) or None."""
        page_url = f"https://www.infineon.com/part/{opn}"
        try:
            resp = self._request(page_url)
            if resp is None:
                print(f"[{self.site_id}] Failed to fetch page for OPN={opn}")
                return None

            html_content = resp.text
            soup = BeautifulSoup(html_content, "html.parser")

            # --- Parse datalayer hidden fields ---
            def _input_val(selector):
                tag = soup.select_one(selector)
                return tag["value"].strip() if tag and tag.get("value") else ""

            ispn_name = _input_val("input#datalayer-ispnName")
            family_name = _input_val("input#datalayer-familyName")
            status = _input_val("input#datalayer-productStatusInfo")

            # Product name: prefer datalayer field, fallback to <h1>
            name = ispn_name or opn
            h1 = soup.find("h1")
            if not name and h1:
                name = h1.get_text(strip=True)

            # Description: meta description, then first product <p>
            description = ""
            meta_desc = soup.find("meta", attrs={"name": "description"})
            if meta_desc and meta_desc.get("content"):
                description = meta_desc["content"].strip()
            if not description:
                for p in soup.find_all("p"):
                    text = p.get_text(strip=True)
                    if len(text) > 30:
                        description = text
                        break

            # Image URL
            image_url = None
            for img in soup.find_all("img"):
                src = img.get("src", "") or img.get("data-src", "")
                if src and ("product" in src.lower() or opn.lower() in src.lower()):
                    image_url = src if src.startswith("http") else f"https://www.infineon.com{src}"
                    break

            # Specs: parse tables with th/td pairs
            specs = {}
            for table in soup.find_all("table"):
                rows = table.find_all("tr")
                for row in rows:
                    cells = row.find_all(["th", "td"])
                    if len(cells) >= 2:
                        key = cells[0].get_text(strip=True)
                        val = cells[1].get_text(strip=True)
                        if key and val:
                            specs[key] = val

            # --- Fetch breadcrumb JSON for category ---
            category_str = self._fetch_breadcrumb(opn)

            product = {
                "id": str(uuid.uuid4()),
                "site_id": self.site_id,
                "external_id": opn,
                "name": name,
                "price": None,
                "price_value": None,
                "currency": None,
                "brand": "Infineon",
                "category": category_str,
                "description": description,
                "image_url": image_url,
                "image_urls": json.dumps([image_url]) if image_url else None,
                "specs": json.dumps(specs),
                "rating": None,
                "review_count": None,
                "availability": status or None,
                "url": page_url,
                "html_path": None,  # filled by caller
                "metadata": json.dumps({"family": family_name}),
            }
            return product, html_content

        except Exception as e:
            print(f"[{self.site_id}] Error processing OPN={opn}: {e}")
            return None

    def _fetch_breadcrumb(self, opn):
        """Fetch breadcrumb JSON and build category string."""
        url = f"https://www.infineon.com/dataApi/en/part.ptpbreadcrumb.en.{opn}.json"
        try:
            resp = self._request(url)
            if resp is None:
                return ""
            data = resp.json()

            # Navigate hierarchy: data may be a list or dict
            # Structure: [{productFamilyId, name, path, ...}, ...]
            parts = []
            if isinstance(data, list):
                for item in data:
                    name = item.get("name") or item.get("categoryName") or ""
                    if name:
                        parts.append(name)
            elif isinstance(data, dict):
                # Walk nested parent structure
                node = data
                for _ in range(8):
                    name = node.get("name") or node.get("categoryName") or ""
                    if name:
                        parts.append(name)
                    parent = node.get("parent") or node.get("children")
                    if not parent or not isinstance(parent, dict):
                        break
                    node = parent

            return " > ".join(parts) if parts else ""
        except Exception as e:
            print(f"[{self.site_id}] Breadcrumb error for OPN={opn}: {e}")
            return ""

    def _save_html(self, opn, html_content):
        """Save already-fetched HTML to archive directory and return path."""
        try:
            html_path = os.path.abspath(os.path.join(self._archive_dir, f"{opn}.html"))
            with open(html_path, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(html_content)
            return html_path
        except Exception as e:
            print(f"[{self.site_id}] Failed to save HTML for OPN={opn}: {e}")
            return None
