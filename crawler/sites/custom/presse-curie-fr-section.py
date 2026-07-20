# -*- coding: utf-8 -*-
"""Crawler for presse.curie.fr/section/communiques (Institut Curie press releases).

The listing page renders articles via WordPress AJAX (admin-ajax.php).
The WP REST API is disabled and the RSS feed has malformed XML, so we drive
the custom AJAX flux:
  - First batch:  action=mon_action_Post
  - Subsequent:   action=load_more_post  with offset=N*posts_per_page
A fresh nonce is scraped from the listing page on every crawl run.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape
from urllib.parse import unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class PresseCurieFrSectionCrawler(BaseCrawler):
    site_id = "presse-curie-fr-section"
    site_name = "Custom: presse-curie-fr-section"
    base_url = "https://presse.curie.fr"

    AJAX_URL = "https://presse.curie.fr/wp-admin/admin-ajax.php"
    LIST_PAGE_URL = "https://presse.curie.fr/section/communiques/?lang=en"
    CATEGORY_NAME = "communiques"
    POSTS_PER_PAGE = 10
    BACKOFF = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50
    MAX_PAGES = 200

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl communiqués and save to DB. Returns count of saved items."""
        start_time = time.monotonic()
        MAX_WALL_SECS = 25 * 60

        nonce = self._fetch_nonce()
        if not nonce:
            print(f"[{self.site_id}] ERROR: could not fetch nonce from listing page")
            return 0

        saved = 0
        seen_urls: set = set()
        page = 0

        try:
            while True:
                if time.monotonic() - start_time > MAX_WALL_SECS:
                    print(f"[{self.site_id}] 25-minute budget exhausted; stopping cleanly")
                    break

                if limit is not None and saved >= limit:
                    break

                if page >= self.MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self.MAX_PAGES} pages reached; stopping")
                    break

                offset = page * self.POSTS_PER_PAGE
                list_html = self._fetch_list_page(nonce, offset)

                if not list_html:
                    print(f"[{self.site_id}] Empty list response at offset {offset}; stopping")
                    break

                records = self._parse_list_html(list_html)
                if not records:
                    print(f"[{self.site_id}] No records at offset {offset}; stopping")
                    break

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                for record in records:
                    if limit is not None and saved >= limit:
                        break

                    url = record.get("url", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    try:
                        time.sleep(self.detail_delay)
                        detail_raw = self._curl_get(url, context=f"detail {url[:80]}")
                        if not detail_raw:
                            raise RuntimeError("detail fetch failed after retries")

                        detail_soup = self._make_soup(detail_raw, context=url)
                        if detail_soup is None:
                            raise RuntimeError("could not parse detail page")

                        parsed = self._parse_detail(detail_soup, record)
                        abstract = (parsed.get("abstract") or "").strip()
                        if len(abstract) < self.MIN_ABSTRACT_CHARS:
                            print(
                                f"[{self.site_id}] skip {url}: "
                                f"abstract too short ({len(abstract)} chars)"
                            )
                            continue

                        self._save_paper({
                            "site_id": self.site_id,
                            "external_id": parsed["external_id"],
                            "post_number": parsed["post_number"],
                            "title": parsed["title"],
                            "abstract": abstract,
                            "published_date": parsed["published_date"],
                            "posted_date": parsed["listed_date"],
                            "url": parsed["url"],
                            "pdf_url": parsed["pdf_url"],
                            "authors": parsed["authors"],
                            "publisher": parsed["publisher"],
                            "department": parsed["department"],
                            "category": parsed["category"],
                            "keywords": parsed["keywords"],
                            "doi": parsed["doi"],
                            "original_filename": parsed["original_filename"],
                            "metadata": json.dumps(parsed["metadata"], ensure_ascii=False),
                        })
                        saved += 1
                        print(f"[{self.site_id}] saved {saved}: {parsed['title'][:80]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {url} failed: {exc}")
                        continue

                page += 1

        except KeyboardInterrupt:
            print(f"[{self.site_id}] interrupted; saved so far: {saved}")
            raise

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # List fetching
    # ------------------------------------------------------------------

    def _fetch_nonce(self):
        """Load the listing page and extract the WP AJAX nonce."""
        raw = self._curl_get(self.LIST_PAGE_URL, context="listing page for nonce")
        if not raw:
            return None
        # Primary: ajaxurlPostNonce = {"url":"...","nonce":"XXXX"}
        m = re.search(r'ajaxurlPostNonce\s*=\s*\{[^}]*"nonce"\s*:\s*"([a-f0-9]+)"', raw)
        if m:
            return m.group(1)
        # Fallback: any nonce-shaped value
        m = re.search(r'"nonce"\s*:\s*"([a-f0-9]{8,12})"', raw)
        if m:
            return m.group(1)
        return None

    def _fetch_list_page(self, nonce: str, offset: int):
        """POST to admin-ajax.php and return the HTML fragment, or None."""
        action = "mon_action_Post" if offset == 0 else "load_more_post"
        data = "&".join([
            f"action={action}",
            f"query_args%5Bcategory_name%5D={self.CATEGORY_NAME}",
            f"posts_per_page={self.POSTS_PER_PAGE}",
            f"offset={offset}",
            "type=loadmore",
            "load_more_is_activated=1",
            "total=9999",
            f"security={nonce}",
            "hash=crawlerabc",
        ])
        raw = self._curl_post(self.AJAX_URL, data=data, context=f"list offset={offset}")
        if not raw:
            return None
        # WordPress returns "0" or empty string when there are no more posts
        if raw.strip() in ("0", "", "false"):
            return None
        return raw

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_html(self, html: str) -> list:
        """Parse the AJAX HTML fragment into [{url, title, post_id}, ...]."""
        soup = self._make_soup(html, context="list fragment")
        if soup is None:
            return []
        records = []
        for div in soup.find_all("div", class_="item-row"):
            # Post ID from CSS class "post-44383"
            post_id = None
            for cls in div.get("class", []):
                m = re.match(r"^post-(\d+)$", cls)
                if m:
                    post_id = m.group(1)
                    break

            link = div.select_one(".content-post a[href]") or div.select_one("a[href]")
            if not link:
                continue
            url = (link.get("href") or "").strip()
            if not url:
                continue
            if not url.startswith("http"):
                url = urljoin(self.base_url, url)

            title_el = div.select_one(".entry-title") or div.select_one("h3")
            title = self._node_text(title_el) or (link.get("title") or "").strip()
            if not title:
                continue

            records.append({"url": url, "title": title, "post_id": post_id})
        return records

    def _parse_detail(self, soup: BeautifulSoup, record: dict) -> dict:
        """Extract metadata from a detail page soup."""
        url = (
            self._meta_content(soup, "og:url")
            or self._link_href(soup, "link[rel='canonical']")
            or record.get("url", "")
        )
        if url and not url.startswith("http"):
            url = urljoin(self.base_url, url)

        # Title
        h1 = soup.find("h1")
        title = (
            self._node_text(h1)
            or self._meta_content(soup, "og:title")
            or record.get("title", "")
        )
        title = self._strip_site_suffix(title)

        # Post number from list record or trailing numeric ID in URL slug
        post_id = record.get("post_id") or self._extract_trailing_id(url)

        # Published date: machine-readable meta preferred
        pub_date = self._meta_content(soup, "article:published_time")
        if pub_date:
            pub_date = pub_date[:10]  # YYYY-MM-DD only
        else:
            date_el = soup.select_one("p.date")
            pub_date = self._parse_date(self._node_text(date_el))
        listed_date = pub_date

        # Category from ".date-container" e.g. "Press Release 3 June 2026"
        cat_raw = self._node_text(soup.select_one(".date-container")) or ""
        category = re.sub(r"\s*\d.*$", "", cat_raw).strip() or "Press Release"

        # Abstract: full article body
        content_el = soup.select_one(".templateCp-content") or soup.select_one(".entry-content")
        if content_el:
            abstract = self._extract_text(content_el)
        else:
            abstract = ""
        if len(abstract) < self.MIN_ABSTRACT_CHARS:
            abstract = self._meta_content(soup, "og:description", "description") or abstract

        # PDF / download attachment
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if not href:
                continue
            href_lower = href.lower()
            if ".pdf" in href_lower or "/download" in href_lower or "picid=" in href_lower:
                pdf_url = urljoin(self.base_url, href)
                n_m = re.search(r"[?&]n=([^&]+)", href)
                if n_m:
                    fname = unquote(n_m.group(1))
                    if not fname.lower().endswith(".pdf"):
                        fname += ".pdf"
                    original_filename = fname
                else:
                    fname = href.split("/")[-1].split("?")[0]
                    original_filename = fname if fname else None
                break

        # DOI
        doi = ""
        doi_m = re.search(r"https?://(?:dx\.)?doi\.org/(10\.[^\s\"'<>]+)", str(soup))
        if doi_m:
            doi = doi_m.group(1).rstrip(".,)")

        external_id = post_id or urlparse(url).path.strip("/") or url

        return {
            "external_id": external_id,
            "post_number": post_id,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "listed_date": listed_date,
            "url": url,
            "pdf_url": pdf_url,
            "authors": "",
            "publisher": "Institut Curie",
            "department": "",
            "category": category,
            "keywords": "",
            "doi": doi,
            "original_filename": original_filename,
            "metadata": {
                "posted_date": pub_date,
                "category_raw": cat_raw,
                "canonical_url": url,
                "og_description": self._meta_content(soup, "og:description"),
            },
        }

    def _extract_text(self, container) -> str:
        """Return clean plain text from a BeautifulSoup element."""
        try:
            inner = BeautifulSoup(str(container), "html.parser")
        except Exception:
            return self._node_text(container)
        for tag in inner.find_all(["script", "style", "noscript", "iframe"]):
            tag.decompose()
        parts = []
        for el in inner.find_all(["p", "li", "h2", "h3", "h4", "blockquote"]):
            t = self._node_text(el)
            if t and t not in parts:
                parts.append(t)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, context: str = "GET") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", f"Referer: {self.base_url}/",
            url,
        ]
        return self._run_curl(cmd, context)

    def _curl_post(self, url: str, data: str, context: str = "POST") -> str | None:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: */*",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", "Content-Type: application/x-www-form-urlencoded",
            "-H", f"Referer: {self.LIST_PAGE_URL}",
            "--data", data,
            url,
        ]
        return self._run_curl(cmd, context)

    def _run_curl(self, cmd: list, context: str) -> str | None:
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10)
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(err or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} curl attempt {attempt}/3: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF[attempt - 1]
                    print(f"[{self.site_id}] retrying in {wait}s...")
                    time.sleep(wait)
        print(f"[{self.site_id}] {context} failed after 3 attempts: {last_error}")
        return None

    def _make_soup(self, raw, context: str = "HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        return None

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _node_text(node) -> str:
        if node is None:
            return ""
        text = node.get_text(" ", strip=True)
        text = unescape(text)
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @staticmethod
    def _meta_content(soup, *keys: str) -> str:
        for key in keys:
            m = (
                soup.find("meta", attrs={"property": key})
                or soup.find("meta", attrs={"name": key})
            )
            if m and m.get("content"):
                return m["content"].strip()
        return ""

    @staticmethod
    def _link_href(soup, selector: str) -> str:
        n = soup.select_one(selector)
        return (n.get("href") or "").strip() if n else ""

    @staticmethod
    def _strip_site_suffix(title: str) -> str:
        title = re.sub(r"\s*[-|]\s*Institut Curie.*$", "", title, flags=re.I)
        title = re.sub(r"\s*[-|]\s*Espace presse.*$", "", title, flags=re.I)
        return title.strip()

    @staticmethod
    def _extract_trailing_id(url: str):
        """Extract trailing numeric ID from slug like /slug-name-44383/."""
        m = re.search(r"-(\d{3,})/?$", url)
        return m.group(1) if m else None

    MONTHS = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "janvier": 1, "février": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "août": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
        "fevrier": 2, "aout": 8,
    }

    @classmethod
    def _parse_date(cls, text: str) -> str:
        if not text:
            return ""
        text_lower = text.lower().strip()
        iso = re.search(r"\b((?:19|20)\d{2})-(\d{2})-(\d{2})\b", text_lower)
        if iso:
            return iso.group(0)
        m = re.search(r"\b(\d{1,2})\s+(\w[\wÀ-ž]*)\s+((?:19|20)\d{2})\b", text_lower)
        if m:
            month = cls.MONTHS.get(m.group(2))
            if month:
                return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
        return ""
