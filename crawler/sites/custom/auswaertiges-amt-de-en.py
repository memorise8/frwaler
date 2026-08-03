# -*- coding: utf-8 -*-
"""Crawler for the Federal Foreign Office (Auswärtiges Amt) English newsroom."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class AuswaertigesAmtDeEnCrawler(BaseCrawler):
    site_id = "auswaertiges-amt-de-en"
    site_name = "Custom: auswaertiges-amt-de-en"
    base_url = "https://www.auswaertiges-amt.de"

    _LIST_URL = (
        "https://www.auswaertiges-amt.de"
        "/ajax/json-filterlist/en/newsroom/news/609204-609204"
    )
    _PAGE_SIZE = 20
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_RUNTIME_SECS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
    _MIN_ABSTRACT_CHARS = 100
    _BACKOFF = (1, 3, 9)
    _CURL_TIMEOUT = 60

    def crawl(self, limit=None):
        """Crawl the Federal Foreign Office English newsroom."""
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_display = str(limit) if limit is not None else "∞"

        for page in range(self._MAX_PAGES):
            if limit is not None and saved >= limit:
                break

            elapsed = time.time() - start_time
            if elapsed > self._MAX_RUNTIME_SECS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached, stopping cleanly.")
                break

            if page == self._MAX_PAGES - 1:
                print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached, stopping.")

            offset = page * self._PAGE_SIZE

            if page > 0 and page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

            list_raw = self._curl_get(f"{self._LIST_URL}?offset={offset}")
            if not list_raw:
                print(f"[{self.site_id}] list fetch failed at offset={offset}, stopping.")
                break

            try:
                data = json.loads(list_raw)
                items = data.get("items") or []
            except (json.JSONDecodeError, AttributeError) as exc:
                print(f"[{self.site_id}] JSON error at offset={offset}: {exc}")
                break

            if not items:
                print(f"[{self.site_id}] No more items at offset={offset}. Done.")
                break

            new_on_page = 0

            for item in items:
                if limit is not None and saved >= limit:
                    break

                link = item.get("link") or ""
                if not link:
                    continue

                detail_url = urljoin(self.base_url, link)
                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)
                new_on_page += 1

                try:
                    slug = link.rstrip("/").split("/")[-1]
                    m = re.search(r"(\d{5,})", slug)
                    external_id = m.group(1) if m else slug

                    list_date_raw = item.get("date") or ""

                    time.sleep(self._delay)

                    detail_raw = self._curl_get(
                        detail_url,
                        referer="https://www.auswaertiges-amt.de/en/newsroom/news",
                    )
                    if not detail_raw:
                        raise RuntimeError(f"detail fetch failed for {detail_url}")

                    detail = self._parse_detail(detail_raw, item)

                    abstract = detail.get("abstract") or ""
                    if len(abstract) < self._MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] skipped {link}: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    paper = {
                        "id": None,
                        "site_id": self.site_id,
                        "external_id": external_id,
                        "post_number": external_id,
                        "title": detail.get("title") or item.get("headline") or "",
                        "authors": "",
                        "abstract": abstract,
                        "category": item.get("name") or item.get("type") or "",
                        "keywords": detail.get("keywords") or "",
                        "published_date": (
                            detail.get("published_date")
                            or self._parse_date(list_date_raw)
                        ),
                        "listed_date": self._parse_date(list_date_raw),
                        "url": detail_url,
                        "pdf_url": detail.get("pdf_url") or "",
                        "doi": "",
                        "department": "Federal Foreign Office",
                        "publisher": "Federal Foreign Office",
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "posted_date": list_date_raw,
                                "type": item.get("type") or "",
                                "name": item.get("name") or "",
                                "snippet": item.get("text") or "",
                                "link": link,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    counter = f"{saved}/{limit}" if limit is not None else str(saved)
                    print(f"[{self.site_id}] saved {counter}: {paper['title'][:70]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {link!r} failed: {exc}")
                    continue

            if new_on_page == 0:
                print(f"[{self.site_id}] Page {page}: all items already seen. Done.")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, referer=None):
        """GET via curl with retry + exponential backoff. Returns text or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--max-time", str(self._CURL_TIMEOUT),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/json,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
        ]
        if referer:
            cmd += ["-H", f"Referer: {referer}"]
        cmd.append(url)

        for attempt, wait in enumerate(self._BACKOFF):
            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    timeout=self._CURL_TIMEOUT + 10,
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if result.returncode == 0 and text.strip():
                    return text
                if attempt < len(self._BACKOFF) - 1:
                    print(
                        f"[{self.site_id}] curl empty/error for {url}, "
                        f"retry in {wait}s"
                    )
                    time.sleep(wait)
            except subprocess.TimeoutExpired:
                if attempt < len(self._BACKOFF) - 1:
                    print(
                        f"[{self.site_id}] curl timeout for {url}, retry in {wait}s"
                    )
                    time.sleep(wait)
            except Exception as exc:
                if attempt < len(self._BACKOFF) - 1:
                    print(
                        f"[{self.site_id}] curl error for {url}: {exc}, "
                        f"retry in {wait}s"
                    )
                    time.sleep(wait)

        print(f"[{self.site_id}] Failed after {len(self._BACKOFF)} attempts: {url}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw):
        """Parse HTML with fallback parsers."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception as exc:
                print(f"[{self.site_id}] HTML parse failed with {parser}: {exc}")
        return None

    def _parse_detail(self, raw, list_item):
        """Parse a detail page; return dict with title/abstract/published_date/keywords/pdf_url."""
        try:
            soup = self._make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] soup construction failed: {exc}")
            return {}
        if soup is None:
            return {}

        main = (
            soup.find("main")
            or soup.find(id="main")
            or soup.find(id="content")
            or soup
        )

        # --- Title ---
        title = ""
        h1 = main.select_one("h1.heading__title")
        if h1:
            span = h1.select_one("span.heading__title-text")
            title = self._clean_text(
                span.get_text(" ", strip=True) if span else h1.get_text(" ", strip=True)
            )
        if not title or "Welcome" in title:
            for h in main.find_all("h1"):
                t = self._clean_text(h.get_text(" ", strip=True))
                if t and "Welcome" not in t:
                    title = t
                    break
        if not title:
            title = self._clean_text(list_item.get("headline") or "")

        # --- Published date from heading__meta (format: "DD.MM.YYYY - Type") ---
        published_date = ""
        meta_span = main.select_one("span.heading__meta")
        if meta_span:
            published_date = self._parse_date(meta_span.get_text(" ", strip=True))

        # --- Lead/intro paragraph ---
        intro = ""
        intro_p = main.select_one("p.heading__intro")
        if intro_p:
            intro_text = self._clean_text(intro_p.get_text(" ", strip=True))
            # Only use intro if it's meaningful (not just a publication date line)
            if intro_text and not re.match(r"^Published on\b", intro_text):
                intro = intro_text

        # --- Article body: paragraphs with class rte__paragraph inside main ---
        body_texts = []
        for p in main.find_all("p", class_="rte__paragraph"):
            t = self._clean_text(p.get_text(" ", strip=True))
            if (
                t
                and t not in ("Print page", "Share page", "Back")
                and "Print page" not in t
                and "Share page" not in t
                and "Back to:" not in t
                and "You are here:" not in t
                and "Report an accessibility" not in t
            ):
                body_texts.append(t)

        # Fallback: paragraphs in first u-grid-row when rte__paragraph yields nothing useful
        if not body_texts:
            for div in main.find_all("div", class_="u-grid-row"):
                paras = div.find_all("p")
                for p in paras:
                    t = self._clean_text(p.get_text(" ", strip=True))
                    if (
                        t
                        and "Print page" not in t
                        and "Share page" not in t
                        and "Back to:" not in t
                        and "You are here:" not in t
                        and len(t) > 20
                    ):
                        body_texts.append(t)
                if body_texts:
                    break

        # Combine intro + body, truncate to ~3000 chars
        parts = []
        if intro:
            parts.append(intro)
        parts.extend(body_texts)
        abstract = " ".join(parts)
        if len(abstract) > 3000:
            abstract = abstract[:3000]

        # --- Keywords ---
        keywords = ""
        for sec in main.find_all("section"):
            sec_text = sec.get_text(" ", strip=True)
            if "Keywords" in sec_text or "Schlagworte" in sec_text:
                kw_links = [
                    a.get_text(strip=True)
                    for a in sec.find_all("a")
                    if a.get_text(strip=True)
                ]
                if kw_links:
                    keywords = ", ".join(kw_links)
                else:
                    raw_kw = re.sub(r"^Keywords?\s*", "", sec_text).strip()
                    keywords = raw_kw
                break

        # --- PDF link ---
        pdf_url = ""
        pdf_a = main.select_one('a[href$=".pdf"]') or main.select_one('a[download]')
        if pdf_a:
            href = pdf_a.get("href") or ""
            if href:
                pdf_url = urljoin(self.base_url, href)

        return {
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "keywords": keywords,
            "pdf_url": pdf_url,
        }

    @staticmethod
    def _parse_date(raw):
        """Parse DD.MM.YYYY or YYYY-MM-DD from a string. Returns YYYY-MM-DD or ''."""
        if not raw:
            return ""
        m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", raw)
        if m:
            return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        if m:
            return m.group(1)
        return ""

    @staticmethod
    def _clean_text(text):
        if not text:
            return ""
        return re.sub(r"\s+", " ", text).strip()
