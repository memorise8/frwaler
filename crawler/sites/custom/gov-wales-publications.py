# -*- coding: utf-8 -*-
"""Crawler for GOV.WALES publications index.

Starting URL: https://www.gov.wales/publications
Pagination:   ?page=N  (0-indexed, 10 items per page, ~1637 pages)
Detail pages: Drupal 11 HTML, abstract from #description-block or hero summary.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class GovWalesPublicationsCrawler(BaseCrawler):
    site_id = "gov-wales-publications"
    site_name = "Custom: gov-wales-publications"
    base_url = "https://www.gov.wales"

    LIST_URL = "https://www.gov.wales/publications"
    MAX_PAGES = 200
    MAX_RUNTIME_SECONDS = 25 * 60
    RUNTIME_GRACE_SECONDS = 30
    PUBLISHER = "Welsh Government"

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, *, timeout: int = 45) -> Optional[str]:
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            url,
        ]
        last_error = ""
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=timeout + 5, check=False
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and body.strip():
                    return body
                last_error = (
                    result.stderr.decode("utf-8", errors="replace").strip()
                    or f"curl exit {result.returncode}"
                )
            except subprocess.TimeoutExpired as exc:
                last_error = f"timeout after {exc.timeout}s"
            except OSError as exc:
                last_error = str(exc)

            if attempt < 2:
                wait = waits[attempt]
                print(
                    f"[{self.site_id}] fetch failed for {url} "
                    f"(attempt {attempt+1}/3): {last_error}; retrying in {wait}s"
                )
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts for {url}: {last_error}")
        return None

    def _make_soup(self, raw: str) -> BeautifulSoup:
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw or "", parser)
            except Exception as exc:
                print(f"[{self.site_id}] parser {parser} failed: {exc}")
        return BeautifulSoup("", "html.parser")

    # ------------------------------------------------------------------
    # Text / date utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _clean(text: str) -> str:
        text = (text or "").replace("\xa0", " ")
        return re.sub(r"\s+", " ", text).strip()

    @staticmethod
    def _parse_date(raw: str) -> str:
        raw = (raw or "").strip()
        m = re.match(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        for fmt in ("%d %B %Y", "%d %b %Y", "%B %Y", "%b %Y"):
            try:
                return datetime.strptime(raw, fmt).date().isoformat()
            except ValueError:
                pass
        return ""

    # ------------------------------------------------------------------
    # List page parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, raw: str) -> List[Dict[str, Any]]:
        soup = self._make_soup(raw)
        records: List[Dict[str, Any]] = []

        for item in soup.select(".index-list__item"):
            a = item.select_one(".index-list__title a")
            if not a or not a.get("href"):
                continue
            href = a["href"]
            url = href if href.startswith("http") else urljoin(self.base_url, href)
            title = self._clean(a.get_text(" "))
            if not title or not url:
                continue

            time_el = item.select_one(".index-list__date time")
            listed_date = ""
            if time_el:
                listed_date = (
                    self._parse_date(time_el.get("datetime", ""))
                    or self._parse_date(self._clean(time_el.get_text(" ")))
                )

            type_el = item.select_one(".index-list__type")
            pub_type = self._clean(type_el.get_text(" ")) if type_el else ""

            topics = [
                self._clean(t.get_text(" "))
                for t in item.select(".index-list__topics .from-area")
                if self._clean(t.get_text(" "))
            ]

            slug = urlsplit(url).path.strip("/").split("/")[-1]

            records.append({
                "title": title,
                "url": url,
                "slug": slug,
                "listed_date": listed_date,
                "pub_type": pub_type,
                "topics": topics,
            })

        return records

    # ------------------------------------------------------------------
    # Abstract extraction
    # ------------------------------------------------------------------

    def _extract_abstract(self, soup: BeautifulSoup, main: Any) -> str:
        parts: List[str] = []
        seen: set = set()

        def add(text: str) -> None:
            t = self._clean(text)
            if t and len(t) > 20 and t not in seen:
                seen.add(t)
                parts.append(t)

        # 1. #description-block — full details section (best source)
        desc_block = main.select_one("#description-block")
        if desc_block:
            for h in desc_block.find_all(["h2", "h3", "h4"]):
                h.decompose()
            for btn in desc_block.find_all(class_="btn--summary"):
                btn.decompose()
            add(desc_block.get_text(" "))

        # 2. hero-block summary paragraphs (excluding "Read details" button)
        hero = main.select_one(".hero-block__summary")
        if hero:
            for p in hero.find_all("p"):
                if "btn--summary" in (p.get("class") or []):
                    continue
                add(p.get_text(" "))

        # 3. meta description
        meta_desc = soup.find("meta", attrs={"name": "description"})
        if meta_desc and meta_desc.get("content"):
            add(meta_desc["content"])

        # 4. Extra body paragraphs if still short
        combined = " ".join(parts)
        if len(combined) < 100:
            for p in main.find_all("p"):
                cls_str = " ".join(p.get("class") or [])
                if "visually-hidden" in cls_str or "btn--summary" in cls_str:
                    continue
                add(p.get_text(" "))
                combined = " ".join(parts)
                if len(combined) >= 100:
                    break

        return self._clean(" ".join(parts))

    # ------------------------------------------------------------------
    # Detail page parsing
    # ------------------------------------------------------------------

    def _parse_detail(
        self, url: str, raw: str, listing: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        soup = self._make_soup(raw)
        main = soup.find("main") or soup

        # Title
        h1 = main.select_one(".page-header__title")
        title = self._clean(h1.get_text(" ")) if h1 else listing["title"]
        if not title:
            title = listing["title"]

        # Abstract
        abstract = self._extract_abstract(soup, main)
        if len(abstract) < 100:
            print(
                f"[{self.site_id}] short abstract ({len(abstract)} chars) "
                f"for {url}; skipping"
            )
            return None

        # Published date (first-published block on detail page)
        fp_el = main.select_one(".first-published .item")
        published_date = (
            self._parse_date(self._clean(fp_el.get_text(" "))) if fp_el else ""
        )
        if not published_date:
            published_date = listing["listed_date"]

        listed_date = listing["listed_date"]

        # PDFs — prefer explicit PDF type marker, fallback to .pdf href
        pdf_url = ""
        original_filename = ""
        for doc in main.select(".document"):
            dtype = doc.select_one(".document__type")
            if dtype and "PDF" in dtype.get_text(strip=True).upper():
                a = doc.select_one("a[href]")
                if a and a.get("href"):
                    href = a["href"]
                    pdf_url = (
                        href if href.startswith("http") else urljoin(self.base_url, href)
                    )
                    original_filename = pdf_url.split("/")[-1].split("?")[0]
                    break
        if not pdf_url:
            for a in main.find_all("a", href=True):
                href = a["href"]
                if href.lower().endswith(".pdf"):
                    pdf_url = (
                        href if href.startswith("http") else urljoin(self.base_url, href)
                    )
                    original_filename = pdf_url.split("/")[-1].split("?")[0]
                    break

        # Category / type
        type_el = main.select_one(".page-header__type")
        category = (
            self._clean(type_el.get_text(" ")) if type_el else listing.get("pub_type", "")
        )

        # Keywords: publication type + topics
        kw_list = ([category] if category else []) + [
            t for t in listing.get("topics", []) if t
        ]
        seen_kw: set = set()
        unique_kw: List[str] = []
        for k in kw_list:
            if k.lower() not in seen_kw:
                seen_kw.add(k.lower())
                unique_kw.append(k)
        keywords = ", ".join(unique_kw)

        # Breadcrumbs for metadata
        breadcrumbs = [
            self._clean(b.get_text(" ")) for b in soup.select(".breadcrumb li a")
        ]

        slug = listing["slug"]

        metadata = {
            "posted_date": listed_date,
            "pub_type": listing.get("pub_type"),
            "topics": listing.get("topics", []),
            "breadcrumbs": breadcrumbs,
            "originalFilename": original_filename or None,
        }

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "url": url,
            "pdf_url": pdf_url or None,
            "original_filename": original_filename or None,
            "publisher": self.PUBLISHER,
            "keywords": keywords,
            "category": category,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        page = 0  # 0-indexed
        seen_urls: set = set()
        limit_or_inf = limit if limit is not None else "inf"
        started = time.monotonic()

        while page < self.MAX_PAGES:
            if limit is not None and saved >= limit:
                break

            elapsed = time.monotonic() - started
            if elapsed >= self.MAX_RUNTIME_SECONDS - self.RUNTIME_GRACE_SECONDS:
                print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                break

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            list_url = f"{self.LIST_URL}?page={page}"
            raw = self._curl_get(list_url)
            if not raw:
                print(f"[{self.site_id}] page {page}: empty list response; stopping")
                break

            records = self._parse_list_page(raw)
            if not records:
                print(f"[{self.site_id}] page {page}: 0 records; stopping")
                break

            # Deduplicate
            new_records: List[Dict[str, Any]] = []
            for rec in records:
                key = rec["url"].rstrip("/")
                if not key or key in seen_urls:
                    continue
                seen_urls.add(key)
                new_records.append(rec)

            if not new_records:
                print(f"[{self.site_id}] page {page}: 0 new records (all duplicates); stopping")
                break

            for idx, rec in enumerate(new_records, start=1):
                if limit is not None and saved >= limit:
                    break

                elapsed = time.monotonic() - started
                if elapsed >= self.MAX_RUNTIME_SECONDS - self.RUNTIME_GRACE_SECONDS:
                    print(f"[{self.site_id}] approaching 25 minute budget; exiting cleanly")
                    return saved

                try:
                    time.sleep(self._delay)
                    detail_raw = self._curl_get(rec["url"])
                    if not detail_raw:
                        print(f"[{self.site_id}] item {idx}: empty detail for {rec['url']}")
                        continue

                    paper = self._parse_detail(rec["url"], detail_raw, rec)
                    if not paper:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_or_inf}: "
                        f"{paper['title'][:80]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            page += 1

        if page >= self.MAX_PAGES:
            print(f"[{self.site_id}] reached safety cap of {self.MAX_PAGES} pages")

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
