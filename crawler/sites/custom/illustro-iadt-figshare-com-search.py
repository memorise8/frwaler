# -*- coding: utf-8 -*-
"""Crawler for illustro-iadt.figshare.com thesis search (item_type=8).

Uses the public figshare API (api.figshare.com/v2) — the portal itself is
behind AWS WAF / JS challenge so direct HTML scraping is not possible.
IADT's group_id in figshare is 54031; item_type=8 is "thesis".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class IllustroIadtFigshareCrawler(BaseCrawler):
    site_id   = "illustro-iadt-figshare-com-search"
    site_name = "Custom: illustro-iadt-figshare-com-search"
    base_url  = "https://illustro-iadt.figshare.com"

    _API_BASE  = "https://api.figshare.com/v2"
    _GROUP_ID  = 54031   # IADT's figshare group
    _ITEM_TYPE = 8       # thesis
    _PAGE_SIZE = 100     # max allowed by figshare API
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))     # safety cap

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_json(self, url, *, timeout=60):
        """Fetch *url* via curl, return parsed JSON or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 10, check=False
                )
            except Exception as exc:
                last_error = str(exc)
            else:
                if result.returncode == 0 and result.stdout:
                    raw = result.stdout.decode("utf-8", errors="replace")
                    try:
                        return json.loads(raw)
                    except json.JSONDecodeError as exc:
                        print(f"[{self.site_id}] JSON decode error for {url}: {exc}")
                        return None
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} {stderr}"

            if attempt < 2:
                print(
                    f"[{self.site_id}] curl retry {attempt+1}/3 for {url}: "
                    f"{last_error}; retrying in {waits[attempt]}s"
                )
                time.sleep(waits[attempt])

        print(f"[{self.site_id}] curl failed after 3 attempts for {url}: {last_error}")
        return None

    def _fetch_list_page(self, page):
        url = (
            f"{self._API_BASE}/articles"
            f"?item_type={self._ITEM_TYPE}"
            f"&group={self._GROUP_ID}"
            f"&page_size={self._PAGE_SIZE}"
            f"&page={page}"
            f"&order=published_date&order_direction=desc"
        )
        return self._curl_json(url)

    def _fetch_detail(self, article_id):
        return self._curl_json(f"{self._API_BASE}/articles/{article_id}")

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _strip_html(html_text):
        """Strip HTML tags, return plain text."""
        if not html_text:
            return ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                soup = BeautifulSoup(html_text, parser)
                text = soup.get_text(separator=" ")
                return re.sub(r"\s+", " ", text).strip()
            except Exception:
                continue
        # Fallback: regex stripping
        return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html_text))).strip()

    @staticmethod
    def _parse_date(dt_str):
        """Extract YYYY-MM-DD from an ISO datetime string."""
        if not dt_str:
            return None
        m = re.match(r"(\d{4}-\d{2}-\d{2})", str(dt_str))
        return m.group(1) if m else None

    @staticmethod
    def _custom_field(custom_fields, name):
        """Return value of a named figshare custom_field."""
        for cf in (custom_fields or []):
            if cf.get("name") == name:
                val = cf.get("value")
                if isinstance(val, list):
                    return "; ".join(str(v) for v in val if v)
                return str(val).strip() if val else None
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls = set()
        start_time = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        for page in range(1, self._MAX_PAGES + 1):
            if time.time() - start_time > max_wall:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; exiting cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page % 10 == 0:
                limit_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            items = self._fetch_list_page(page)
            if items is None:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break
            if not isinstance(items, list) or not items:
                print(f"[{self.site_id}] page {page}: empty response; end of results")
                break
            if page == self._MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self._MAX_PAGES} pages reached; stopping")

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                article_id = item.get("id")
                if not article_id:
                    continue

                pub_url = (
                    item.get("url_public_html")
                    or item.get("figshare_url")
                    or f"{self.base_url}/articles/thesis/{article_id}"
                )
                if pub_url in seen_urls:
                    continue
                seen_urls.add(pub_url)
                new_on_page += 1

                try:
                    detail = self._fetch_detail(article_id)
                    if not detail:
                        print(f"[{self.site_id}] item {article_id} failed: empty detail")
                        continue

                    title = (detail.get("title") or item.get("title") or "").strip()
                    if not title:
                        print(f"[{self.site_id}] item {article_id} skipped: no title")
                        continue

                    abstract = self._strip_html(detail.get("description") or "")
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {article_id} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Authors
                    authors_list = [
                        a.get("full_name", "").strip()
                        for a in (detail.get("authors") or [])
                        if a.get("full_name", "").strip()
                    ]
                    authors = "; ".join(authors_list)

                    # Keywords / tags
                    tags = detail.get("tags") or detail.get("keywords") or []
                    keywords = ", ".join(str(t) for t in tags if t)

                    # Dates
                    timeline = detail.get("timeline") or {}
                    published_date = self._parse_date(
                        detail.get("published_date")
                        or timeline.get("publisherPublication")
                    )
                    # listed_date → stored as posted_date (libertree adapter reads it)
                    listed_date = self._parse_date(
                        timeline.get("firstOnline") or timeline.get("posted")
                    )

                    # Public URL
                    url = (
                        detail.get("figshare_url")
                        or detail.get("url_public_html")
                        or pub_url
                    )

                    # PDF file
                    pdf_url = None
                    original_filename = None
                    for f in (detail.get("files") or []):
                        mime = f.get("mimetype") or ""
                        name = f.get("name") or ""
                        if mime == "application/pdf" or name.lower().endswith(".pdf"):
                            pdf_url = f.get("download_url")
                            original_filename = name or None
                            break

                    doi = detail.get("doi") or ""
                    handle = detail.get("handle") or ""

                    custom_fields = detail.get("custom_fields") or []
                    faculty      = self._custom_field(custom_fields, "Faculty")
                    research_area = self._custom_field(custom_fields, "Research Area")
                    thesis_type  = self._custom_field(custom_fields, "Thesis Type")
                    publisher    = (
                        self._custom_field(custom_fields, "Contributor affiliation")
                        or "Institute of Art, Design & Technology"
                    )

                    metadata = {
                        "article_id": article_id,
                        "handle": handle,
                        "group_id": detail.get("group_id"),
                        "defined_type_name": detail.get("defined_type_name"),
                        "supervisor": self._custom_field(custom_fields, "Supervisor"),
                        "submission_date": self._custom_field(custom_fields, "Submission date"),
                        "format": self._custom_field(custom_fields, "Format"),
                        "faculty": faculty,
                        "license": (detail.get("license") or {}).get("name"),
                        "version": detail.get("version"),
                        "citation": detail.get("citation"),
                        "posted_date": timeline.get("posted"),
                        "firstOnline": timeline.get("firstOnline"),
                        "originalFilename": original_filename,
                    }

                    paper = {
                        "site_id":           self.site_id,
                        "external_id":       str(article_id),
                        "post_number":       str(article_id),
                        "title":             title,
                        "abstract":          abstract,
                        "authors":           authors,
                        "publisher":         publisher,
                        "department":        faculty,
                        "journal":           thesis_type,
                        "url":               url,
                        "pdf_url":           pdf_url,
                        "original_filename": original_filename,
                        "keywords":          keywords,
                        "category":          research_area,
                        "doi":               doi,
                        "published_date":    published_date,
                        "posted_date":       listed_date,
                        "metadata":          json.dumps(metadata, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    limit_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {article_id} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
