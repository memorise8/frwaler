# -*- coding: utf-8 -*-
"""Product crawler that extends GenericCrawler for product page scraping."""

import json
import os
import re
import uuid

from bs4 import BeautifulSoup
from .generic_crawler import GenericCrawler
from . import db


class ProductCrawler(GenericCrawler):
    """A crawler specialized for product pages with HTML archiving."""

    def __init__(self, config_path, db_conn, delay=None):
        super().__init__(config_path, db_conn, delay)
        self._html_archive_dir = os.path.join(
            os.path.dirname(__file__), '..', 'html_archive', self.site_id
        )
        if self._config.get("options", {}).get("save_html", True):
            os.makedirs(self._html_archive_dir, exist_ok=True)
        # Ensure products table exists
        db.init_db(db_conn)

    def crawl(self, limit=None):
        """Override crawl to use product extraction and storage."""
        crawl_type = self._config.get("crawl_type", "html")
        try:
            if crawl_type == "single-page":
                return self._crawl_products_single_page(limit)
            return self._crawl_products_html(limit)
        finally:
            self._close_browser()

    def _crawl_products_html(self, limit=None):
        """Crawl product list pages and detail pages."""
        list_cfg = self._config.get("list_page", {})
        detail_cfg = self._config.get("detail_page", {})
        selectors = list_cfg.get("selectors", {})
        pagination = list_cfg.get("pagination", {})

        page_param = pagination.get("param", "page")
        page_num = pagination.get("start", 1)
        max_pages = pagination.get("max_pages", 10)
        saved = 0
        skipped = 0

        db.register_site(self._db, self.site_id, self.site_name, self.base_url)

        for _ in range(max_pages):
            if limit is not None and saved >= limit:
                break

            params = dict(list_cfg.get("params", {}))
            if pagination.get("type") == "query_param":
                params[page_param] = page_num

            soup = self._fetch_page(list_cfg["url"], params=params)
            if soup is None:
                print(f"[{self.site_id}] Failed to fetch list page {page_num}. Stopping.")
                break

            items = self._extract_list_items(soup, selectors)
            if not items:
                print(f"[{self.site_id}] No items found on page {page_num}. Stopping.")
                break

            print(f"[{self.site_id}] Page {page_num}: found {len(items)} items")

            for item_info in items:
                if limit is not None and saved >= limit:
                    break

                detail_url = item_info.get("detail_url")
                if detail_url and detail_cfg.get("selectors"):
                    product = self._fetch_product_detail(detail_url, item_info, detail_cfg)
                else:
                    product = self._build_product_from_list(item_info)

                if product:
                    result = self._save_product(product)
                    if result:
                        saved += 1
                        label = f"{saved}/{limit}" if limit else str(saved)
                        print(f"[{self.site_id}] Saved {label} products...")
                    else:
                        skipped += 1

            page_num += pagination.get("step", 1)

        print(f"[{self.site_id}] Done: {saved} new, {skipped} duplicates skipped.")
        return saved

    def _crawl_products_single_page(self, limit=None):
        """Crawl products from a single page."""
        list_cfg = self._config.get("list_page", {})
        detail_cfg = self._config.get("detail_page", {})
        selectors = list_cfg.get("selectors", {})
        saved = 0
        skipped = 0

        db.register_site(self._db, self.site_id, self.site_name, self.base_url)

        soup = self._fetch_page(list_cfg["url"])
        if soup is None:
            print(f"[{self.site_id}] Failed to fetch page.")
            return 0

        items = self._extract_list_items(soup, selectors)
        print(f"[{self.site_id}] Found {len(items)} items")

        for item_info in items:
            if limit is not None and saved >= limit:
                break

            detail_url = item_info.get("detail_url")
            if detail_url and detail_cfg.get("selectors"):
                product = self._fetch_product_detail(detail_url, item_info, detail_cfg)
            else:
                product = self._build_product_from_list(item_info)

            if product:
                result = self._save_product(product)
                if result:
                    saved += 1
                else:
                    skipped += 1

        print(f"[{self.site_id}] Done: {saved} new, {skipped} duplicates skipped.")
        return saved

    def _fetch_product_detail(self, url, basic_info, detail_cfg):
        """Fetch product detail page, archive HTML, extract product fields."""
        soup = self._fetch_page(url)
        if soup is None:
            return None

        sel = detail_cfg.get("selectors", {})
        external_id = basic_info.get("external_id") or db.compute_content_hash(
            basic_info.get("title", ""), url
        )

        # Archive raw HTML
        html_path = None
        if self._config.get("options", {}).get("save_html", True):
            html_path = self._save_html(soup, external_id)

        # Extract product fields
        name = basic_info.get("title", "")
        if sel.get("name"):
            tag = soup.select_one(sel["name"])
            if tag:
                name = tag.get_text(strip=True)

        price_text = ""
        if sel.get("price"):
            tag = soup.select_one(sel["price"])
            if tag:
                price_text = tag.get_text(strip=True)

        price_value, currency = self._parse_price(price_text)

        brand = ""
        if sel.get("brand"):
            tag = soup.select_one(sel["brand"])
            if tag:
                brand = tag.get_text(strip=True)

        category = basic_info.get("category", "")
        if sel.get("category"):
            tag = soup.select_one(sel["category"])
            if tag:
                category = tag.get_text(strip=True)

        description = ""
        if sel.get("description"):
            tag = soup.select_one(sel["description"])
            if tag:
                description = tag.get_text(separator="\n", strip=True)

        image_url = ""
        if sel.get("image"):
            tag = soup.select_one(sel["image"])
            if tag:
                image_url = self._make_absolute(
                    tag.get("src") or tag.get("data-src") or tag.get("content", "")
                )

        image_urls = []
        if sel.get("images"):
            for tag in soup.select(sel["images"]):
                src = tag.get("src") or tag.get("data-src") or ""
                if src:
                    image_urls.append(self._make_absolute(src))

        specs = {}
        if sel.get("specs"):
            specs = self._extract_specs(soup, sel["specs"])

        rating = None
        if sel.get("rating"):
            tag = soup.select_one(sel["rating"])
            if tag:
                rating = self._parse_rating(tag.get_text(strip=True))

        review_count = None
        if sel.get("review_count"):
            tag = soup.select_one(sel["review_count"])
            if tag:
                review_count = self._parse_int(tag.get_text(strip=True))

        availability = ""
        if sel.get("availability"):
            tag = soup.select_one(sel["availability"])
            if tag:
                availability = tag.get_text(strip=True)

        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": external_id,
            "name": name,
            "price": price_text,
            "price_value": price_value,
            "currency": currency,
            "brand": brand,
            "category": category,
            "description": description,
            "image_url": image_url,
            "image_urls": json.dumps(image_urls, ensure_ascii=False) if image_urls else None,
            "specs": json.dumps(specs, ensure_ascii=False) if specs else None,
            "rating": rating,
            "review_count": review_count,
            "availability": availability,
            "url": url,
            "html_path": html_path,
            "metadata": None,
        }

    def _build_product_from_list(self, item_info):
        """Build a minimal product dict from list page data only."""
        url = item_info.get("detail_url", "")
        external_id = item_info.get("external_id") or db.compute_content_hash(
            item_info.get("title", ""), url
        )
        return {
            "id": str(uuid.uuid4()),
            "site_id": self.site_id,
            "external_id": external_id,
            "name": item_info.get("title", ""),
            "price": item_info.get("price", ""),
            "price_value": None,
            "currency": None,
            "brand": None,
            "category": item_info.get("category", ""),
            "description": None,
            "image_url": None,
            "image_urls": None,
            "specs": None,
            "rating": None,
            "review_count": None,
            "availability": None,
            "url": url,
            "html_path": None,
            "metadata": None,
        }

    def _save_product(self, product_dict):
        """Save product to database, returns True if new."""
        if db.product_exists(self._db, self.site_id,
                             url=product_dict.get("url"),
                             external_id=product_dict.get("external_id")):
            return False
        db.upsert_product(self._db, product_dict)
        return True

    def _save_html(self, soup, external_id):
        """Save raw HTML to archive directory."""
        safe_id = re.sub(r'[^\w\-.]', '_', str(external_id))[:100]
        filename = f"{safe_id}.html"
        filepath = os.path.join(self._html_archive_dir, filename)
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(str(soup))
            return filepath
        except Exception as e:
            print(f"[{self.site_id}] Failed to save HTML: {e}")
            return None

    def _parse_price(self, text):
        """Parse price text into (numeric_value, currency)."""
        if not text:
            return None, None
        currency_map = {
            '₩': 'KRW', '원': 'KRW',
            '$': 'USD',
            '€': 'EUR',
            '£': 'GBP',
            '¥': 'JPY', '円': 'JPY',
        }
        currency = None
        for symbol, code in currency_map.items():
            if symbol in text:
                currency = code
                break
        # Extract numeric value
        numbers = re.findall(r'[\d,]+\.?\d*', text)
        if numbers:
            try:
                value = float(numbers[0].replace(',', ''))
                return value, currency
            except ValueError:
                pass
        return None, currency

    def _extract_specs(self, soup, selector):
        """Extract spec/attribute table as a dict."""
        specs = {}
        elements = soup.select(selector)
        for el in elements:
            # Try table row pattern: th/td
            th = el.select_one("th")
            td = el.select_one("td")
            if th and td:
                key = th.get_text(strip=True)
                val = td.get_text(strip=True)
                if key:
                    specs[key] = val
                continue
            # Try dt/dd pattern
            dt = el.select_one("dt")
            dd = el.select_one("dd")
            if dt and dd:
                key = dt.get_text(strip=True)
                val = dd.get_text(strip=True)
                if key:
                    specs[key] = val
                continue
            # Try key: value text pattern
            text = el.get_text(strip=True)
            if ':' in text or '：' in text:
                parts = re.split(r'[:：]', text, maxsplit=1)
                if len(parts) == 2:
                    specs[parts[0].strip()] = parts[1].strip()
        return specs

    def _parse_rating(self, text):
        """Parse rating from text like '4.5', '4.5/5', '90%'."""
        if not text:
            return None
        # Percentage pattern
        pct = re.search(r'(\d+(?:\.\d+)?)\s*%', text)
        if pct:
            return round(float(pct.group(1)) / 20, 1)  # Convert to 5-scale
        # Numeric pattern
        num = re.search(r'(\d+(?:\.\d+)?)', text)
        if num:
            return float(num.group(1))
        return None

    def _parse_int(self, text):
        """Parse integer from text, stripping commas and non-digits."""
        if not text:
            return None
        numbers = re.findall(r'[\d,]+', text)
        if numbers:
            try:
                return int(numbers[0].replace(',', ''))
            except ValueError:
                pass
        return None
