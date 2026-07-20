# -*- coding: utf-8 -*-
"""Crawler for Norwegian Institute of Public Health (FHI) English publications.

List API: GET /api/search?type=con-32%2Fcat-750&lang=en&sort=1&page=N&pageSize=25
  Returns JSON with totalMatching, results[{id, title, description, url, type,
  dateCreated, dateChanged, ...}]

Detail: GET https://www.fhi.no{url}
  Server-rendered Vue HTML; <p> tags in <main> contain the abstract text.
  PDF links via href="*.pdf" in contentassets paths.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class FhiNoEnCrawler(BaseCrawler):
    site_id = "fhi-no-en"
    site_name = "Custom: fhi-no-en"
    base_url = "https://www.fhi.no"

    _LIST_API = "https://www.fhi.no/api/search"
    _TYPE_PARAM = "type=con-32%2Fcat-750"
    _PAGE_SIZE = 25

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: application/json,text/html,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            url,
        ]
        waits = [1, 3, 9]
        last_error = "unknown"
        for attempt in range(3):
            try:
                res = subprocess.run(cmd, capture_output=True, timeout=timeout + 10, check=False)
                if res.returncode == 0 and res.stdout:
                    return res.stdout.decode("utf-8", errors="replace")
                last_error = f"exit={res.returncode}"
            except Exception as exc:
                last_error = str(exc)
            if attempt < 2:
                wait = waits[attempt]
                print(f"[{self.site_id}] curl attempt {attempt+1}/3 failed for {url}: {last_error}; retrying in {wait}s")
                time.sleep(wait)
        print(f"[{self.site_id}] curl failed after 3 attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helper
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception:
                continue
        return None

    # ------------------------------------------------------------------
    # List API
    # ------------------------------------------------------------------

    def _fetch_list_page(self, page):
        url = (
            f"{self._LIST_API}?"
            f"{self._TYPE_PARAM}&lang=en&sort=1"
            f"&page={page}&pageSize={self._PAGE_SIZE}"
        )
        raw = self._curl(url)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            print(f"[{self.site_id}] JSON decode failed for list page {page}")
            return None

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _isodate(dt_str):
        if not dt_str:
            return ""
        m = re.search(r"\d{4}-\d{2}-\d{2}", dt_str)
        return m.group(0) if m else ""

    def _fetch_detail(self, detail_url):
        raw = self._curl(detail_url)
        if not raw:
            return {}

        try:
            soup = self._parse_html(raw)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for {detail_url}: {exc}")
            return {}
        if soup is None:
            return {}

        result = {}

        # Abstract: collect <p> text from <main> section
        main = soup.find("main") or soup.find(id="main") or soup.body or soup
        paragraphs = []
        seen_texts: set = set()
        for p in main.find_all("p"):
            text = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()
            if len(text) >= 30 and text not in seen_texts:
                seen_texts.add(text)
                paragraphs.append(text)

        abstract = "\n\n".join(paragraphs)

        # Fallback to meta tags when paragraphs are sparse
        if len(abstract) < 50:
            for attr, name in [("property", "og:description"), ("name", "description")]:
                meta = soup.find("meta", attrs={attr: name})
                if meta and meta.get("content", "").strip():
                    abstract = meta["content"].strip()
                    break

        result["abstract"] = abstract

        # PDF links (contentassets or any .pdf href)
        pdf_urls = []
        for a in soup.find_all("a", href=True):
            href = str(a["href"])
            if re.search(r"\.pdf(?:$|[?#])", href, re.I):
                full = href if href.startswith("http") else urljoin(self.base_url, href)
                if full not in pdf_urls:
                    pdf_urls.append(full)
        result["pdf_url"] = pdf_urls[0] if pdf_urls else ""
        result["pdf_urls"] = pdf_urls

        # Schema.org structured data for author / publisher
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                schema = json.loads(script.string or "")
                if isinstance(schema, dict) and "author" in schema:
                    result["schema_author"] = schema["author"]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 1
        seen_urls: set = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "inf"

        while True:
            # Wall-clock budget: 25 minutes
            if time.time() - start_time > 25 * 60:
                print(f"[{self.site_id}] 25-minute budget reached; stopping cleanly")
                break

            if limit is not None and saved >= limit:
                break

            if page > 1:
                time.sleep(self._delay)

            data = self._fetch_list_page(page)
            if not data:
                print(f"[{self.site_id}] list page {page} failed; stopping")
                break

            results = data.get("results", [])
            total = data.get("totalMatching", 0)

            if not results:
                print(f"[{self.site_id}] no more results at page {page}; done")
                break

            if page == 1:
                print(f"[{self.site_id}] total matching: {total}")

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            for item in results:
                if limit is not None and saved >= limit:
                    break

                item_url = item.get("url", "")
                if not item_url:
                    continue
                detail_url = urljoin(self.base_url, item_url)

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                try:
                    time.sleep(self._delay)
                    detail = self._fetch_detail(detail_url)

                    abstract = (detail.get("abstract") or "").strip()
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] skipping (abstract {len(abstract)} chars): "
                            f"{item.get('title', '')[:60]}"
                        )
                        continue

                    item_id = str(item.get("id", ""))
                    title = item.get("title", "").strip()
                    if not title:
                        print(f"[{self.site_id}] skipping (no title): {detail_url}")
                        continue

                    published_date = self._isodate(item.get("dateCreated", ""))
                    listed_date = self._isodate(item.get("dateChanged", ""))

                    pdf_url = detail.get("pdf_url", "")
                    original_filename = None
                    if pdf_url:
                        tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
                        if "." in tail:
                            original_filename = tail

                    schema_author = detail.get("schema_author", "")
                    if isinstance(schema_author, dict):
                        schema_author = schema_author.get("name", "")
                    authors = str(schema_author).strip() if schema_author else ""

                    paper = {
                        "site_id": self.site_id,
                        "external_id": item_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "posted_date": published_date,
                        "url": detail_url,
                        "pdf_url": pdf_url,
                        "publisher": "Norwegian Institute of Public Health",
                        "authors": authors,
                        "department": "Norwegian Institute of Public Health",
                        "category": item.get("type", ""),
                        "keywords": "",
                        "original_filename": original_filename,
                        "metadata": json.dumps({
                            "posted_date": published_date,
                            "listed_date": listed_date,
                            "dateCreated": item.get("dateCreated", ""),
                            "dateChanged": item.get("dateChanged", ""),
                            "node_id": item_id,
                            "contentType": "con-32",
                            "category": item.get("type", ""),
                            "isArchived": item.get("isArchived", False),
                            "pdf_urls": detail.get("pdf_urls", []),
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item_url} failed: {exc}")
                    continue

            if limit is not None and saved >= limit:
                break

            # Pagination: stop when last page is shorter than page size
            page_size = data.get("pageSize", self._PAGE_SIZE)
            if len(results) < page_size:
                print(f"[{self.site_id}] last page (got {len(results)} < {page_size}); done")
                break

            if page >= 200:
                print(f"[{self.site_id}] safety cap of 200 pages reached; stopping")
                break

            page += 1

        print(f"[{self.site_id}] done. total saved: {saved}")
        return saved
