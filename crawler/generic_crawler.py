# -*- coding: utf-8 -*-
"""Config-driven generic crawler that reads JSON config files."""

import json
import os
import re
import uuid

from bs4 import BeautifulSoup
from .base_crawler import BaseCrawler
from .network_capture import find_data_api


class GenericCrawler(BaseCrawler):
    """A crawler driven by a JSON configuration file."""

    def __init__(self, config_path, db_conn, delay=None):
        with open(config_path, encoding="utf-8") as f:
            self._config = json.load(f)
        _delay = delay or self._config.get("options", {}).get("delay", 1.5)
        _respect_robots = self._config.get("options", {}).get("respect_robots", True)
        super().__init__(db_conn, _delay, respect_robots=_respect_robots)
        self._config_path = config_path
        self._last_diagnosis = []
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

            # Feature 7: Check for iframe content in browser mode
            iframe_html = self._resolve_iframe_browser(page)
            html = iframe_html if iframe_html else page.content()
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
    # Failure diagnosis
    # ------------------------------------------------------------------

    def _diagnose_failure(self, url, soup, selectors):
        """Diagnose why no items were found."""
        hints = []

        text_len = len(soup.get_text(strip=True))

        # Check 1: Page too small (JS rendering needed)
        if text_len < 500:
            hints.append("JS_RENDERING: 페이지 콘텐츠가 매우 적음 - fetch_method를 'browser'로 변경 필요")

        # Check 2: iframes
        iframes = soup.select("iframe[src]")
        if iframes:
            iframe_srcs = [f.get("src", "")[:60] for f in iframes[:3]]
            hints.append(f"IFRAME: iframe {len(iframes)}개 발견 - {', '.join(iframe_srcs)}")

        # Check 3: Suggest alternative selectors
        container_sel = selectors.get("item_container", "")
        alternatives = []
        for sel in ["table tbody tr", "ul li", "div.list-item", "li", "article", "div.item", "div.board-list li", "tr"]:
            if sel != container_sel:
                found = soup.select(sel)
                if 3 <= len(found) <= 100:
                    alternatives.append(f"'{sel}' ({len(found)}개)")
        if alternatives:
            hints.append(f"SELECTOR: 대체 셀렉터 후보 - {', '.join(alternatives[:5])}")

        # Check 4: Page has links
        all_links = soup.select("a[href]")
        if len(all_links) > 10:
            hints.append(f"LINKS: 페이지에 링크 {len(all_links)}개 존재 - 셀렉터 수정 필요")

        return hints

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
        saved = 0
        skipped = 0

        while True:
            if limit is not None and saved >= limit:
                break

            # Build list page URL with pagination
            params = dict(list_cfg.get("params", {}))
            if pagination.get("type") == "query_param":
                params[page_param] = page_num

            soup = self._fetch_page(list_cfg["url"], params=params)
            if soup is None:
                print(f"[{self.site_id}] Failed to fetch list page {page_num}. Stopping.")
                break
            # Feature 7: Resolve iframe if main content is inside one
            soup, _ = self._resolve_iframe(soup, list_cfg["url"])

            items = self._extract_list_items(soup, selectors)

            # Feature 6: AJAX/API auto-detection fallback
            if not items and self._config.get("options", {}).get("auto_detect_api", True):
                list_url = list_cfg["url"]
                if params and pagination.get("type") == "query_param":
                    from urllib.parse import urlencode
                    list_url = list_url + ("&" if "?" in list_url else "?") + urlencode(params)
                print(f"[{self.site_id}] No items found via HTML on page {page_num}, trying API detection...")
                try:
                    api_result = find_data_api(list_url)
                    if api_result:
                        print(f"[{self.site_id}] Found API: {api_result['url'][:80]}")
                        items = self._parse_api_response(api_result)
                except Exception as e:
                    print(f"[{self.site_id}] API detection failed: {e}")

            if not items:
                if page_num == pagination.get("start", 1):
                    hints = self._diagnose_failure(list_cfg["url"], soup, selectors)
                    self._last_diagnosis = hints
                    if hints:
                        print(f"[{self.site_id}] Failure diagnosis:")
                        for hint in hints:
                            print(f"[{self.site_id}]   - {hint}")
                print(f"[{self.site_id}] No items found on page {page_num}. Stopping.")
                break

            print(f"[{self.site_id}] Page {page_num}: found {len(items)} items")

            for item_info in items:
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
                    result = self._save_paper(paper)
                    if result:
                        saved += 1
                        label = f"{saved}/{limit}" if limit else str(saved)
                        print(f"[{self.site_id}] Saved {label} papers...")
                    else:
                        skipped += 1

            page_num += pagination.get("step", 1)

        print(f"[{self.site_id}] Done: {saved} new, {skipped} duplicates skipped.")
        return saved

    def _extract_list_items(self, soup, selectors):
        """Extract items from a list page using CSS selectors."""
        items = []
        container_sel = selectors.get("item_container", "tr")
        rows = soup.select(container_sel)

        for row in rows:
            # Extract link to detail page
            link_sel = selectors.get("item_link", "a[href]")
            if link_sel == "self":
                link_tag = row
            else:
                link_tag = row.select_one(link_sel)
            if not link_tag:
                continue

            link_attr = selectors.get("item_link_attr", "href")
            if link_attr == "text":
                href = link_tag.get_text(strip=True)
            else:
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
                date = date_tag.get_text(strip=True) if date_tag else ""

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

        # Feature 7: Resolve iframe if main content is inside one
        soup, url = self._resolve_iframe(soup, url)

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
                published_date = self._get_tag_value(tag)
                # Remove common label prefixes (e.g., "날짜 :", "Date:")
                published_date = re.sub(r"^(날짜|Date|작성일|등록일)\s*[:：]\s*", "", published_date)

        # File download link (PDF, Excel, HWP, CSV, etc.)
        pdf_url = ""
        if sel.get("pdf_link"):
            tag = soup.select_one(sel["pdf_link"])
            if tag:
                pdf_attr = sel.get("pdf_link_attr", "href")
                pdf_url = self._make_absolute(tag.get(pdf_attr, ""))

        # If no specific pdf_link selector matched, try finding any downloadable file
        if not pdf_url and sel.get("file_link"):
            tag = soup.select_one(sel["file_link"])
            if tag:
                file_attr = sel.get("file_link_attr", "href")
                pdf_url = self._make_absolute(tag.get(file_attr, ""))

        # Auto-detect: if still no link, look for common download patterns
        if not pdf_url and sel.get("auto_detect_files"):
            file_extensions = ('.pdf', '.xlsx', '.xls', '.csv', '.hwp', '.docx', '.doc', '.pptx', '.ppt', '.zip', '.json', '.xml')
            for a_tag in soup.select("a[href]"):
                href = a_tag.get("href", "").lower()
                if any(href.endswith(ext) for ext in file_extensions):
                    pdf_url = self._make_absolute(a_tag.get("href", ""))
                    break

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
    # Feature 6: AJAX/API auto-detection
    # ------------------------------------------------------------------

    def _parse_api_response(self, api_result):
        """Parse items from a detected API response."""
        items = []
        try:
            data = json.loads(api_result["sample"])

            # Find the list of items
            item_list = None
            if isinstance(data, list):
                item_list = data
            elif isinstance(data, dict):
                for key in ["items", "data", "list", "result", "results", "rows",
                            "content", "records", "body", "resultList", "bbsList",
                            "nttList", "boardList", "dataList"]:
                    if key in data and isinstance(data[key], list):
                        item_list = data[key]
                        break
                if not item_list:
                    for v in data.values():
                        if isinstance(v, dict):
                            for v2 in v.values():
                                if isinstance(v2, list) and len(v2) >= 3:
                                    item_list = v2
                                    break

            if not item_list:
                return items

            for item in item_list:
                if not isinstance(item, dict):
                    continue

                # Try to extract title
                title = ""
                for k in ["title", "nttSj", "bbsSj", "sj", "subject", "boardTitle",
                           "artclTitle", "cn", "name", "nm"]:
                    if k in item and item[k]:
                        title = str(item[k]).strip()
                        break

                if not title:
                    # Use first string value > 10 chars as title
                    for v in item.values():
                        if isinstance(v, str) and len(v) > 10:
                            title = v.strip()
                            break

                # Try to extract URL
                url = ""
                for k in ["url", "link", "href", "detailUrl", "articleUrl", "nttUrl"]:
                    if k in item and item[k]:
                        url = str(item[k]).strip()
                        break

                # Try to extract date
                date = ""
                for k in ["date", "regDate", "registDt", "frstRegstDt", "createDt",
                           "publishDate", "pubDate", "writDt", "rgsDt"]:
                    if k in item and item[k]:
                        date = str(item[k]).strip()[:10]
                        break

                if title:
                    items.append({
                        "detail_url": self._make_absolute(url) if url else "",
                        "external_id": "",
                        "title": title,
                        "published_date": date,
                        "category": "",
                    })

            print(f"[{self.site_id}] Extracted {len(items)} items from API")
        except Exception as e:
            print(f"[{self.site_id}] API parse error: {e}")

        return items

    # ------------------------------------------------------------------
    # Feature 7: iframe crawling
    # ------------------------------------------------------------------

    def _resolve_iframe(self, soup, page_url):
        """If main content is in an iframe, fetch the iframe src and return its soup."""
        main_text = soup.get_text(strip=True)
        iframes = soup.select("iframe[src]")

        if len(main_text) < 500 and iframes:
            for iframe in iframes:
                src = iframe.get("src", "")
                if not src or src.startswith("javascript:") or "google" in src or "facebook" in src:
                    continue

                iframe_url = self._make_absolute(src)
                print(f"[{self.site_id}] Following iframe: {iframe_url[:80]}")

                try:
                    resp = self._request(iframe_url)
                    if resp:
                        return BeautifulSoup(resp.text, "html.parser"), iframe_url
                except Exception as e:
                    print(f"[{self.site_id}] iframe fetch failed: {e}")
                    continue

        return soup, page_url

    def _resolve_iframe_browser(self, page):
        """Check for iframes in Playwright page and switch to iframe content."""
        frames = page.frames
        if len(frames) > 1:
            # Find the largest non-main frame
            for frame in frames[1:]:  # Skip main frame
                try:
                    content = frame.content()
                    if len(content) > 1000:
                        print(f"[{self.site_id}] Switching to iframe: {frame.url[:80]}")
                        return content
                except Exception:
                    continue
        return None

    # ------------------------------------------------------------------
    # Single-page crawling (all links on one page)
    # ------------------------------------------------------------------

    def _crawl_single_page(self, limit=None):
        """Crawl a single page and extract all document links directly."""
        list_cfg = self._config["list_page"]
        selectors = list_cfg.get("selectors", {})

        soup = self._fetch_page(list_cfg["url"])
        if soup is None:
            print(f"[{self.site_id}] Failed to fetch page. Stopping.")
            return 0

        # Find all document links
        link_sel = selectors.get("link", "a[href]")
        link_attr = selectors.get("link_attr", "href")
        links = soup.select(link_sel)

        if not links:
            print(f"[{self.site_id}] No links found with '{link_sel}'. Stopping.")
            return 0

        print(f"[{self.site_id}] Found {len(links)} document links")

        # Category heading selector (nearest parent heading for grouping)
        cat_sel = selectors.get("category_heading", "h2")

        saved = 0
        skipped = 0
        for tag in links:
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
            result = self._save_paper(paper)
            if result:
                saved += 1
                label = f"{saved}/{limit}" if limit else str(saved)
                print(f"[{self.site_id}] Saved {label} items...")
            else:
                skipped += 1

        print(f"[{self.site_id}] Done: {saved} new, {skipped} duplicates skipped.")
        return saved

    # ------------------------------------------------------------------
    # API crawling (placeholder for future)
    # ------------------------------------------------------------------

    def _crawl_api(self, limit=None):
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
