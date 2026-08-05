# -*- coding: utf-8 -*-
"""Crawler for University of Southampton ePrints — NOC division.

Strategy
--------
1. Fetch the division index page → collect year links.
2. For each year (newest first) fetch the flat HTML listing page and
   extract eprint detail-page URLs.
3. For each eprint detail page:
   - Parse <meta name="eprints.*"> tags for structured metadata
     (published_date, DOI, keywords, authors, journal, …).
   - Extract the abstract from the HTML body (not in a meta tag on this
     site) — the pattern is <h2>Abstract</h2><p ...>…</p>.
   - Extract the first PDF href.
   - Save via self._save_paper().

The whole eprints.soton.ac.uk origin is now fronted by Anubis
(https://anubis.techaro.lol), a JS proof-of-work anti-scraper gate — plain
curl gets a 401 "Ensuring the security of your connection" PoW page instead
of real content. A real browser is required to execute the PoW JS. We keep
one headless Playwright page alive for the whole crawl() call: the PoW is
solved once (~8-9s) on the first navigation and the resulting cookie clears
every subsequent same-context navigation almost instantly.
"""

from __future__ import annotations

import json
import os
import re
import time
from html import unescape
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler

_INDEX_URL = (
    "https://eprints.soton.ac.uk/view/divisions/"
    "5d1c5ba6-6977-4d9d-9afe-fd1820201262/"
)
_YEAR_LIST_URL = (
    "https://eprints.soton.ac.uk/view/divisions/"
    "5d1c5ba6-6977-4d9d-9afe-fd1820201262/{year}.html"
)

_WAITS = [1, 3, 9]
_SAFETY_PAGE_CAP = 200
_WALL_CLOCK_BUDGET = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds


class EprintsSotonAcUkViewCrawler(BaseCrawler):
    site_id = "eprints-soton-ac-uk-view"
    site_name = "Custom: eprints-soton-ac-uk-view"
    base_url = "https://eprints.soton.ac.uk"

    # ------------------------------------------------------------------
    # Network helper (persistent Playwright page — Anubis PoW gate)
    # ------------------------------------------------------------------

    def _get_page(self):
        """Lazily launch a single headless-Chromium page for the whole crawl.

        Reused across every list/detail fetch so the Anubis proof-of-work
        challenge (solved once on the first navigation) stays cleared for
        the rest of the run via the context's cookie.
        """
        if getattr(self, "_pw_page", None) is not None:
            return self._pw_page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._pw_browser = self._pw.chromium.launch(headless=True)
        self._pw_context = self._pw_browser.new_context(user_agent=self.USER_AGENT)
        self._pw_page = self._pw_context.new_page()
        return self._pw_page

    def _close_browser(self):
        for attr, closer in (
            ("_pw_context", lambda o: o.close()),
            ("_pw_browser", lambda o: o.close()),
            ("_pw", lambda o: o.stop()),
        ):
            obj = getattr(self, attr, None)
            if obj is not None:
                try:
                    closer(obj)
                except Exception:
                    pass
        self._pw_page = None

    def _curl(self, url: str, *, timeout: int = 60) -> str | None:
        """Fetch *url* with the persistent page, waiting out the Anubis
        proof-of-work challenge if it appears. Returns HTML text or None.
        """
        page = self._get_page()
        last_err = "unknown"
        for attempt in range(3):
            try:
                page.goto(url, timeout=timeout * 1000, wait_until="domcontentloaded")
                for _ in range(14):
                    title = page.title() or ""
                    if "Ensuring the security" not in title:
                        break
                    page.wait_for_timeout(1500)
                # allow a moment for post-challenge redirect/render to settle
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                title = page.title() or ""
                if "Ensuring the security" in title:
                    last_err = "anubis challenge did not clear"
                else:
                    return page.content()
            except Exception as exc:
                last_err = str(exc)

            if attempt < 2:
                wait = _WAITS[attempt]
                print(f"[{self.site_id}] fetch attempt {attempt+1}/3 failed for {url}: {last_err}; retry in {wait}s")
                time.sleep(wait)

        print(f"[{self.site_id}] fetch failed after 3 attempts for {url}: {last_err}")
        return None

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    def _parse_html(self, raw: str) -> BeautifulSoup | None:
        if not raw:
            return None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(raw, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _clean(text: str) -> str:
        if not text:
            return ""
        text = unescape(text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()

    # ------------------------------------------------------------------
    # Collect years from the division index page
    # ------------------------------------------------------------------

    def _get_years(self) -> list[str]:
        raw = self._curl(_INDEX_URL)
        if not raw:
            return []
        soup = self._parse_html(raw)
        if soup is None:
            return []
        years = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.match(r"^(\d{4})\.html$", href)
            if m:
                years.append(m.group(1))
        years.sort(reverse=True)
        return years

    # ------------------------------------------------------------------
    # Collect eprint detail-page URLs from a year listing page
    # ------------------------------------------------------------------

    def _get_eprint_urls_for_year(self, year: str) -> list[str]:
        url = _YEAR_LIST_URL.format(year=year)
        raw = self._curl(url)
        if not raw:
            return []
        soup = self._parse_html(raw)
        if soup is None:
            return []
        urls: list[str] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # absolute URLs like https://eprints.soton.ac.uk/499274/
            m = re.match(r"https://eprints\.soton\.ac\.uk/(\d+)/?$", href)
            if m:
                full = f"https://eprints.soton.ac.uk/{m.group(1)}/"
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
        return urls

    # ------------------------------------------------------------------
    # Parse all <meta name="eprints.*"> tags from a detail page
    # ------------------------------------------------------------------

    @staticmethod
    def _collect_meta(soup: BeautifulSoup) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for tag in soup.find_all("meta", attrs={"name": re.compile(r"^eprints\.")}):
            name = tag.get("name", "")
            val = tag.get("content", "")
            if val:
                result.setdefault(name, []).append(val)
        return result

    @staticmethod
    def _first(meta: dict, key: str) -> str:
        vals = meta.get(key, [])
        return vals[0] if vals else ""

    @staticmethod
    def _all(meta: dict, key: str) -> list[str]:
        return meta.get(key, [])

    # ------------------------------------------------------------------
    # Extract abstract from HTML body (not available as a meta tag)
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_abstract(soup: BeautifulSoup) -> str:
        # Pattern: <h2>Abstract</h2> immediately followed by <p ...>...</p>
        for h2 in soup.find_all("h2"):
            if h2.get_text(strip=True).lower() == "abstract":
                # Walk siblings to find the first <p>
                node = h2.find_next_sibling()
                while node is not None:
                    if node.name == "p":
                        text = node.get_text(separator=" ", strip=True)
                        text = unescape(text)
                        text = re.sub(r"\s+", " ", text).strip()
                        if text:
                            return text
                    elif node.name in ("h1", "h2", "h3", "div", "table"):
                        # Crossed a section boundary without finding text
                        break
                    node = node.find_next_sibling()
        return ""

    # ------------------------------------------------------------------
    # Parse a single eprint detail page
    # ------------------------------------------------------------------

    def _parse_detail_page(self, detail_url: str) -> dict | None:
        raw = self._curl(detail_url)
        if not raw:
            return None
        soup = self._parse_html(raw)
        if soup is None:
            return None

        meta = self._collect_meta(soup)

        eprint_id = self._first(meta, "eprints.eprintid")
        if not eprint_id:
            m = re.search(r"eprints\.soton\.ac\.uk/(\d+)/?", detail_url)
            eprint_id = m.group(1) if m else None

        title = self._clean(self._first(meta, "eprints.title"))
        if not title:
            h1 = soup.find("h1")
            if h1:
                title = self._clean(h1.get_text(separator=" ", strip=True))

        abstract = self._extract_abstract(soup)

        published_date_raw = self._first(meta, "eprints.date")
        datestamp = self._first(meta, "eprints.datestamp")

        published_date = ""
        if published_date_raw:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", published_date_raw)
            if m:
                published_date = m.group(1)
            else:
                m2 = re.match(r"(\d{4})", published_date_raw)
                if m2:
                    published_date = m2.group(1)

        listed_date = ""
        if datestamp:
            m = re.match(r"(\d{4}-\d{2}-\d{2})", datestamp)
            if m:
                listed_date = m.group(1)

        authors_list = [
            self._clean(a)
            for a in self._all(meta, "eprints.creators_name")
            if a.strip()
        ]
        authors = "; ".join(authors_list)

        keywords = self._clean(self._first(meta, "eprints.keywords"))

        publication = self._first(meta, "eprints.publication")
        journal = self._clean(publication)

        doi = self._first(meta, "eprints.doi") or self._first(meta, "eprints.id_number")
        volume = self._first(meta, "eprints.volume")
        issue = self._first(meta, "eprints.number")
        issn = self._first(meta, "eprints.issn")
        item_type = self._first(meta, "eprints.type")
        divisions = "; ".join(self._all(meta, "eprints.divisions"))
        full_text_status = self._first(meta, "eprints.full_text_status")
        series = self._first(meta, "eprints.series")

        # PDF URL: first PDF href found on the page
        pdf_url = ""
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r"\.pdf(\?|$)", href, re.IGNORECASE):
                pdf_url = urljoin(self.base_url, href) if href.startswith("/") else href
                break

        original_filename = None
        if pdf_url:
            tail = pdf_url.rstrip("/").split("/")[-1].split("?")[0]
            if "." in tail and len(tail) <= 200:
                original_filename = tail

        metadata_dict: dict = {
            "node_id": eprint_id,
            "posted_date": datestamp,
            "eprint_type": item_type,
            "divisions": divisions,
            "full_text_status": full_text_status,
            "issn": issn,
        }
        if volume:
            metadata_dict["volume"] = volume
        if issue:
            metadata_dict["issue"] = issue
        if series:
            metadata_dict["series"] = series
        if journal:
            metadata_dict["journal_raw"] = journal
        if doi:
            metadata_dict["doi"] = doi
        if original_filename:
            metadata_dict["originalFilename"] = original_filename

        return {
            "site_id": self.site_id,
            "external_id": eprint_id,
            "post_number": eprint_id,
            "url": detail_url,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": "University of Southampton",
            "journal": journal,
            "keywords": keywords,
            "pdf_url": pdf_url or None,
            "doi": doi or None,
            "department": divisions,
            "category": item_type,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata_dict, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Main crawl loop
    # ------------------------------------------------------------------

    def crawl(self, limit: int | None = None) -> int:
        try:
            return self._crawl_inner(limit)
        finally:
            self._close_browser()

    def _crawl_inner(self, limit: int | None = None) -> int:
        start_time = time.time()
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "∞"

        years = self._get_years()
        if not years:
            print(f"[{self.site_id}] No years found on index page; aborting.")
            return 0
        print(f"[{self.site_id}] Found {len(years)} year pages: {years[0]}–{years[-1]}")

        year_page_count = 0
        for year in years:
            if limit is not None and saved >= limit:
                break

            if year_page_count >= _SAFETY_PAGE_CAP:
                print(f"[{self.site_id}] Safety cap of {_SAFETY_PAGE_CAP} year-pages reached; stopping.")
                break

            elapsed = time.time() - start_time
            if elapsed > _WALL_CLOCK_BUDGET:
                print(f"[{self.site_id}] Wall-clock budget ({_WALL_CLOCK_BUDGET}s) exceeded; stopping.")
                break

            year_page_count += 1
            eprint_urls = self._get_eprint_urls_for_year(year)

            if year_page_count % 10 == 0 or year_page_count == 1:
                print(
                    f"[{self.site_id}] page {year_page_count}: saved {saved}/{limit_str} "
                    f"(year={year}, {len(eprint_urls)} items)"
                )

            if not eprint_urls:
                print(f"[{self.site_id}] year {year}: no eprint URLs found; skipping.")
                continue

            for detail_url in eprint_urls:
                if limit is not None and saved >= limit:
                    break

                if detail_url in seen_urls:
                    continue
                seen_urls.add(detail_url)

                elapsed = time.time() - start_time
                if elapsed > _WALL_CLOCK_BUDGET:
                    print(f"[{self.site_id}] Wall-clock budget exceeded mid-year; stopping.")
                    break

                try:
                    paper = self._parse_detail_page(detail_url)
                    if paper is None:
                        print(f"[{self.site_id}] item {detail_url} failed: could not fetch/parse")
                        continue

                    abstract = paper.get("abstract") or ""
                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] item {detail_url} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {paper.get('title', '')[:80]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                    continue
                finally:
                    time.sleep(self._delay)

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
