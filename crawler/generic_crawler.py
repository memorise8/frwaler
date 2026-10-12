# -*- coding: utf-8 -*-
"""Config-driven generic crawler that reads JSON config files."""

import hashlib
import json
import os
import re
import uuid

from bs4 import BeautifulSoup
from .base_crawler import BaseCrawler
from .enumeration.urls import decode_preencoded


class GenericCrawler(BaseCrawler):
    """A crawler driven by a JSON configuration file."""

    def __init__(self, config_path, db_conn, delay=None):
        with open(config_path, encoding="utf-8") as f:
            self._config = json.load(f)
        _delay = delay or self._config.get("options", {}).get("delay", 1.5)
        super().__init__(db_conn, _delay)
        self._config_path = config_path
        # SSL verification option
        verify_ssl = self._config.get("options", {}).get("verify_ssl", True)
        if not verify_ssl:
            self._session.verify = False
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        # Fetch method: "browser" for SPA, "cloudscraper" for WAF bypass
        self._fetch_method = self._config.get("options", {}).get("fetch_method")
        self._use_browser = self._fetch_method == "browser"
        self._browser = None
        self._playwright = None
        self._cloudscraper = None
        self.delivery_listing_stop = None
        if self._fetch_method == "cloudscraper":
            import cloudscraper
            self._cloudscraper = cloudscraper.create_scraper()

    @property
    def site_id(self):
        return self._config["site_id"]

    @property
    def site_name(self):
        return self._config["site_name"]

    @property
    def base_url(self):
        return self._config["base_url"]

    def crawl(self, limit=None):
        crawl_type = self._config.get("crawl_type", "html")
        try:
            if crawl_type == "api":
                return self._crawl_api(limit)
            if not isinstance(self._config.get("list_page"), dict) or not self._config["list_page"].get("url"):
                self.delivery_listing_stop = "missing_list_configuration"
                self._report_source_failure("configuration")
                return 0
            if crawl_type == "single-page":
                return self._crawl_single_page(limit)
            return self._crawl_html(limit)
        finally:
            self._close_browser()

    # ------------------------------------------------------------------
    # Browser fetch for SPA sites
    # ------------------------------------------------------------------

    def _get_browser_page(self, url, wait_seconds=3):
        """Fetch a page using Playwright headless browser. Returns BeautifulSoup."""
        from playwright.sync_api import sync_playwright

        if self._playwright is None:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=True)

        page = self._browser.new_page(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        try:
            page.goto(url, timeout=30000, wait_until="networkidle")
            page.wait_for_timeout(int(wait_seconds * 1000))
            html = page.content()
        finally:
            page.close()

        encoding = self._config.get("options", {}).get("encoding")
        if encoding:
            html = html.encode(encoding, errors="ignore").decode(encoding, errors="ignore")
        return BeautifulSoup(html, "html.parser")

    def _close_browser(self):
        """Clean up browser resources."""
        if self._browser:
            self._browser.close()
            self._browser = None
        if self._playwright:
            self._playwright.stop()
            self._playwright = None

    def _fetch_page(self, url, params=None):
        """Fetch a page using requests, cloudscraper, or browser based on config."""
        # Never use browser for direct file downloads
        is_file = any(ext in url.lower() for ext in ['.pdf', '.doc', '.docx', '.xls', '.xlsx', '.hwp', '.hwpx'])

        if self._cloudscraper and not is_file:
            try:
                import time as _time
                _time.sleep(self._delay)
                response = self._cloudscraper.get(url, params=params, timeout=30)
                encoding = self._config.get("options", {}).get("encoding")
                if encoding:
                    response.encoding = encoding
                return BeautifulSoup(response.text, "html.parser")
            except Exception as e:
                print(f"[{self.site_id}] Cloudscraper failed: {str(e)[:60]}")
                return None

        if self._use_browser and not is_file:
            # Build URL with params
            if params:
                from urllib.parse import urlencode, urlparse, parse_qs, urlunparse
                parsed = urlparse(url)
                existing = parse_qs(parsed.query)
                existing.update({k: [str(v)] for k, v in params.items()})
                query = urlencode({k: v[0] for k, v in existing.items()})
                url = urlunparse(parsed._replace(query=query))
            try:
                return self._get_browser_page(url)
            except Exception as e:
                print(f"[{self.site_id}] Browser fetch failed: {str(e)[:60]}, falling back to requests")
                # Fall back to requests
                response = self._request(url, params=params)
                if response is None:
                    return None
                return self._parse_response(response)
        else:
            response = self._request(url, params=params)
            if response is None:
                return None
            return self._parse_response(response)

    # ------------------------------------------------------------------
    # HTML crawling (list page → detail page)
    # ------------------------------------------------------------------

    def _crawl_html(self, limit=None):
        list_cfg = self._config["list_page"]
        detail_cfg = self._config.get("detail_page", {})
        pagination = list_cfg.get("pagination", {})
        selectors = list_cfg.get("selectors", {})

        page_num = pagination.get("start", 1)
        page_param = pagination.get("param", "page")
        query_pagination = pagination.get("type") == "query_param"
        step = pagination.get("step", 1)
        if query_pagination and (type(page_num) is not int or type(step) is not int
                                 or step <= 0 or not isinstance(page_param, str) or not page_param.strip()):
            self.delivery_listing_stop = "invalid_pagination_configuration"
            self._report_source_failure("configuration")
            return 0
        saved = 0
        unchanged_pages = 0
        seen_pages = set()

        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit:
                self.delivery_listing_stop = "new_document_limit"
                break

            # Build list page URL with pagination
            params = dict(list_cfg.get("params", {}))
            if self._config.get("options", {}).get("decode_preencoded_params"):
                # Opt-in (plan legacy override): keys such as "type%5B170%5D" are
                # already encoded and requests would send "%255B". Decode once.
                params = decode_preencoded(params)
            if query_pagination:
                params[page_param] = page_num

            soup = self._fetch_page(list_cfg["url"], params=params)
            if soup is None:
                self.delivery_listing_stop = "fetch_failed"
                self._report_source_failure("fetch_failed")
                print(f"[{self.site_id}] Failed to fetch list page {page_num}. Stopping.")
                break
            items = self._extract_list_items(soup, selectors)

            if not items:
                # An empty selector result can mean a changed DOM or a blocked
                # response. It is not evidence of complete source exhaustion.
                self.delivery_listing_stop = "empty_listing_unverified"
                print(f"[{self.site_id}] No items found on page {page_num}. Stopping.")
                break

            fingerprint = hashlib.sha256(json.dumps(sorted({
                item.get("detail_url", "") for item in items
            }), ensure_ascii=False).encode()).digest()
            if fingerprint in seen_pages:
                self.delivery_listing_stop = "repeated_listing"
                self._report_source_failure("invalid_response")
                break
            seen_pages.add(fingerprint)

            print(f"[{self.site_id}] Page {page_num}: found {len(items)} items")
            page_created = 0

            for item_info in items:
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    break

                # Fetch detail page if we have a URL
                detail_url = item_info.get("detail_url")
                if detail_url and detail_cfg.get("selectors"):
                    paper = self._fetch_detail(detail_url, item_info, detail_cfg)
                else:
                    # Use list page data only
                    paper = self._build_paper_from_list(item_info)

                if paper:
                    self._save_paper(paper)
                    if self._last_save_created:
                        saved += 1
                        page_created += 1
                    label = f"{saved}/{limit}" if limit else str(saved)
                    print(f"[{self.site_id}] Saved {label} papers...")
                else:
                    self._report_source_failure("item_failed")

            if limit is not None and saved >= limit:
                self.delivery_listing_stop = "new_document_limit"
                break
            if not query_pagination:
                self.delivery_listing_stop = "pagination_unavailable"
                self._report_source_failure("configuration")
                break

            if self.delivery_mode == "incremental" and self.DELIVERY_ORDER == "newest_first":
                unchanged_pages = unchanged_pages + 1 if page_created == 0 else 0
                stop_pages = self._config.get("options", {}).get("stop_after_unchanged_pages", 2)
                if stop_pages and unchanged_pages >= stop_pages:
                    self.delivery_listing_stop = "unchanged_pages"
                    print(f"[{self.site_id}] {stop_pages} unchanged pages reached; stopping incremental crawl.")
                    break

            page_num += step

        print(f"[{self.site_id}] Done. Saved {saved} papers.")
        return saved

    def _extract_list_items(self, soup, selectors):
        """Extract items from a list page using CSS selectors."""
        items = []
        container_sel = selectors.get("item_container", "tr")
        rows = soup.select(container_sel)

        for row in rows:
            # Extract link to detail page
            link_sel = selectors.get("item_link", "a[href]")
            link_tag = row.select_one(link_sel)
            if not link_tag:
                continue

            link_attr = selectors.get("item_link_attr", "href")
            href = link_tag.get(link_attr, "")
            if not href:
                continue

            # Make absolute URL
            detail_url = self._make_absolute(href)

            # Extract external_id from URL
            external_id = self._extract_id(detail_url)

            # Extract basic info from list page
            title = ""
            title_sel = selectors.get("title")
            if title_sel:
                title_tag = row.select_one(title_sel)
                title = title_tag.get_text(strip=True) if title_tag else ""
            if not title and link_tag:
                title = link_tag.get_text(strip=True)

            date = ""
            date_sel = selectors.get("date")
            if date_sel:
                date_tag = row.select_one(date_sel)
                date = date_tag.get_text(" ", strip=True) if date_tag else ""

            category = ""
            cat_sel = selectors.get("category")
            if cat_sel:
                cat_tag = row.select_one(cat_sel)
                category = cat_tag.get_text(strip=True) if cat_tag else ""

            items.append({
                "detail_url": detail_url,
                "external_id": external_id,
                "title": title,
                "published_date": date,
                "category": category,
            })

        return items

    def _fetch_detail(self, url, basic_info, detail_cfg):
        """Fetch detail page and extract paper fields."""
        soup = self._fetch_page(url)
        if soup is None:
            return None
        sel = detail_cfg.get("selectors", {})

        # Title
        title = basic_info.get("title", "")
        if sel.get("title"):
            tag = soup.select_one(sel["title"])
            if tag:
                title = self._get_tag_value(tag)
                # Remove common label prefixes (e.g., "Title:" from arXiv)
                title = re.sub(r"^(Title|제목)\s*[:：]\s*", "", title)

        # Abstract / content
        abstract = ""
        if sel.get("abstract"):
            tag = soup.select_one(sel["abstract"])
            if tag:
                abstract = self._get_tag_value(tag, mode="separator")

        # Authors
        authors = []
        if sel.get("authors"):
            tags = soup.select(sel["authors"])
            authors = [t.get_text(strip=True) for t in tags if t.get_text(strip=True)]

        # Date
        published_date = basic_info.get("published_date", "")
        if sel.get("date"):
            tag = soup.select_one(sel["date"])
            if tag:
                published_date = self._get_tag_value(tag, "separator")
                # Remove common label prefixes (e.g., "날짜 :", "Date:")
                published_date = re.sub(r"^(날짜|Date|작성일|등록일)\s*[:：]\s*", "", published_date)

        # PDF link
        pdf_url = ""
        if sel.get("pdf_link"):
            tag = soup.select_one(sel["pdf_link"])
            if tag:
                pdf_attr = sel.get("pdf_link_attr", "href")
                pdf_url = self._make_absolute(tag.get(pdf_attr, ""))

        # Keywords
        keywords = []
        if sel.get("keywords"):
            tags = soup.select(sel["keywords"])
            keywords = [t.get_text(strip=True) for t in tags if t.get_text(strip=True)]

        # DOI
        doi = ""
        if sel.get("doi"):
            tag = soup.select_one(sel["doi"])
            if tag:
                doi = tag.get_text(strip=True)

        # Department
        department = ""
        if sel.get("department"):
            tag = soup.select_one(sel["department"])
            if tag:
                department = tag.get_text(strip=True)

        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": basic_info.get("external_id", ""),
            "title": title,
            "authors": json.dumps(authors, ensure_ascii=False),
            "abstract": abstract,
            "category": basic_info.get("category", ""),
            "keywords": json.dumps(keywords, ensure_ascii=False),
            "published_date": published_date,
            "url": url,
            "pdf_url": pdf_url,
            "doi": doi,
            "department": department,
            "metadata": json.dumps({}, ensure_ascii=False),
        }

    def _build_paper_from_list(self, item_info):
        """Build a paper dict from list page data only (no detail page)."""
        return {
            "id": None,
            "site_id": self.site_id,
            "external_id": item_info.get("external_id", ""),
            "title": item_info.get("title", ""),
            "authors": json.dumps([], ensure_ascii=False),
            "abstract": "",
            "category": item_info.get("category", ""),
            "keywords": json.dumps([], ensure_ascii=False),
            "published_date": item_info.get("published_date", ""),
            "url": item_info.get("detail_url", ""),
            "pdf_url": "",
            "doi": "",
            "department": "",
            "metadata": json.dumps({}, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Single-page crawling (all links on one page)
    # ------------------------------------------------------------------

    def _crawl_single_page(self, limit=None):
        """Crawl a single page and extract all document links directly."""
        list_cfg = self._config["list_page"]
        selectors = list_cfg.get("selectors", {})

        soup = self._fetch_page(list_cfg["url"])
        if soup is None:
            self.delivery_listing_stop = "fetch_failed"
            self._report_source_failure("fetch_failed")
            print(f"[{self.site_id}] Failed to fetch page. Stopping.")
            return 0

        # Find all document links
        link_sel = selectors.get("link", "a[href]")
        link_attr = selectors.get("link_attr", "href")
        links = soup.select(link_sel)

        if not links:
            self.delivery_listing_stop = "empty_listing_unverified"
            print(f"[{self.site_id}] No links found with '{link_sel}'. Stopping.")
            return 0

        print(f"[{self.site_id}] Found {len(links)} document links")

        # Category heading selector (nearest parent heading for grouping)
        cat_sel = selectors.get("category_heading", "h2")

        saved = 0
        for tag in links:
            self._check_cancelled()
            if limit is not None and saved >= limit:
                break

            href = tag.get(link_attr, "")
            if not href:
                continue

            url = self._make_absolute(href)
            external_id = self._extract_id(url)
            title = tag.get_text(strip=True)

            # If link text is generic (e.g. "PDF", "Download"), try parent or sibling
            if not title or len(title) < 5:
                parent = tag.parent
                if parent:
                    # Try previous sibling text or parent text
                    prev = tag.find_previous(string=True)
                    if prev and len(prev.strip()) > 5:
                        title = prev.strip()[:200]
                    else:
                        title = parent.get_text(strip=True)[:200]

            # Find category from nearest heading above
            category = ""
            if cat_sel:
                heading = tag.find_previous(cat_sel)
                if heading:
                    category = heading.get_text(strip=True)

            # Clean title - remove common prefixes
            title = re.sub(r"^(Title|제목)\s*[:：]\s*", "", title)

            paper = {
                "id": None,
                "site_id": self.site_id,
                "external_id": external_id,
                "title": title,
                "authors": json.dumps([], ensure_ascii=False),
                "abstract": "",
                "category": category,
                "keywords": json.dumps([], ensure_ascii=False),
                "published_date": "",
                "url": url,
                "pdf_url": url,
                "doi": "",
                "department": "",
                "metadata": json.dumps({}, ensure_ascii=False),
            }
            self._save_paper(paper)
            if self._last_save_created:
                saved += 1
            label = f"{saved}/{limit}" if limit else str(saved)
            print(f"[{self.site_id}] Saved {label} items...")

        self.delivery_listing_stop = "new_document_limit" if limit is not None and saved >= limit else "single_page_unverified"
        print(f"[{self.site_id}] Done. Saved {saved} items.")
        return saved

    # ------------------------------------------------------------------
    # API crawling (placeholder for future)
    # ------------------------------------------------------------------

    def _crawl_api(self, limit=None):
        self.delivery_listing_stop = "api_not_implemented"
        self._report_source_failure("configuration")
        print(f"[{self.site_id}] API crawling not yet implemented for generic configs.")
        return 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _get_tag_value(tag, mode="text"):
        """Extract value from a tag. Handles meta tags (content attr) and regular tags (text)."""
        if tag is None:
            return ""
        # Meta tags store value in 'content' attribute
        if tag.name == "meta":
            return tag.get("content", "").strip()
        if mode == "separator":
            return tag.get_text(separator=" ", strip=True)
        return tag.get_text(strip=True)

    def _parse_response(self, response):
        """Parse response HTML with proper encoding from config."""
        encoding = self._config.get("options", {}).get("encoding")
        if encoding:
            response.encoding = encoding
        return BeautifulSoup(response.text, "html.parser")

    def _make_absolute(self, url):
        """Convert a relative URL to absolute using base_url."""
        if not url:
            return ""
        if url.startswith("http"):
            return url
        if url.startswith("//"):
            return "https:" + url
        if url.startswith("/"):
            return self.base_url.rstrip("/") + url
        return self.base_url.rstrip("/") + "/" + url

    def _extract_id(self, url):
        """Extract an external ID from a URL using config regex or fallback."""
        if self.site_id == "krivet-re-kr":
            from .document_identity import krivet_id
            try:
                return krivet_id(url)
            except ValueError:
                self._source_failure = "invalid_document_id"
                raise
        id_regex = self._config.get("options", {}).get("id_regex")
        if id_regex:
            match = re.search(id_regex, url)
            if match:
                return match.group(1) if match.groups() else match.group(0)
        # Fallback: use last path segment or full URL hash
        parts = url.rstrip("/").split("/")
        if parts:
            last = parts[-1]
            # Try to extract from query params
            if "=" in last:
                for param in last.split("&"):
                    if "=" in param:
                        return param.split("=")[-1]
            return last
        return str(uuid.uuid4())[:8]
