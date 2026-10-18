# -*- coding: utf-8 -*-
"""Crawler for ARN (Allmänna reklamationsnämnden) Årsredovisningar.

Source: https://www.arn.se/om-arn/arsredovisning/
All annual reports are linked as PDFs from a single HTML page — no pagination.
Each entry: year, PDF link, constructed abstract from page preamble + year context.
"""

import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler


class ArnSeOmArnCrawler(BaseCrawler):
    site_id = "arn-se-om-arn"
    site_name = "Custom: arn-se-om-arn"
    base_url = "https://www.arn.se"

    _LIST_URL = "https://www.arn.se/om-arn/arsredovisning/"
    _PUBLISHER = "ARN - Allmänna reklamationsnämnden"

    def _curl_get(self, url: str, retries: int = 3):
        """Fetch URL via curl (TLS-tolerant). Returns decoded string or None."""
        for attempt in range(retries):
            if attempt > 0:
                wait = 3 ** (attempt - 1)
                print(f"[{self.site_id}] Retry {url} in {wait}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-fsk", "--max-time", "30",
                        "-A", self.USER_AGENT,
                        url,
                    ],
                    capture_output=True,
                    timeout=40,
                )
                body = result.stdout
                if result.returncode != 0 or not body:
                    continue
                try:
                    text = body.decode("utf-8")
                except UnicodeDecodeError:
                    text = body.decode("utf-8", errors="replace")
                from delivery.health.access_challenge import access_challenge
                if access_challenge(text):
                    self._report_source_failure("authentication_failed")
                    return None
                return text
            except subprocess.TimeoutExpired:
                print(f"[{self.site_id}] curl timeout: {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl error for {url}: {exc}")
        return None

    @staticmethod
    def _make_soup(html_text: str):
        """Parse HTML with html5lib → lxml → html.parser fallback chain."""
        from bs4 import BeautifulSoup
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(html_text, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _build_abstract(year: str, preamble: str) -> str:
        """Build a >=100-char abstract for an annual report entry."""
        base = (
            f"Allmänna reklamationsnämndens (ARN) årsredovisning för år {year}. "
            f"{preamble} "
            f"Rapporten redogör för myndighetens verksamhetsresultat och ekonomiska "
            f"ställning under {year}."
        ).strip()
        return base

    # Pure enumeration entry points; existing crawl and helpers stay unchanged.
    def _enumeration_items(self, raw, url=None):
        """Rows of the single årsredovisning page, filtered exactly as crawl() does.

        The source has no pagination (crawl() reads one page): any page number
        above 1 is declared empty, so the plan's listing_marker ends the listing.
        A page without PDF links, a repeated reporting year or an unparsable body
        is a listing parse error (crawl() reports invalid_response there).
        """
        from urllib.parse import parse_qs, urlsplit
        from crawler.enumeration.http import PermanentFailure
        from delivery.health.access_challenge import access_challenge
        page = (parse_qs(urlsplit(url or "").query).get("page") or ["1"])[0]
        if not isinstance(raw, str) or access_challenge(raw):
            raise PermanentFailure("listing_parse_error:not_a_listing", url)
        if page != "1":
            return []
        soup = self._make_soup(raw)
        if soup is None:
            raise PermanentFailure("listing_parse_error:html_parse_failed", url)
        preamble = ""
        preamble_tag = soup.find("p", class_="preamble")
        if preamble_tag:
            preamble = preamble_tag.get_text(strip=True)
        if not preamble:
            preamble = (
                "Årsredovisningen innehåller en resultatredovisning samt en "
                "ekonomisk redovisning av ARN:s verksamhet."
            )
        main = (
            soup.find("main")
            or soup.find("div", {"id": "main-content"})
            or soup.find("div", {"id": "content"})
            or soup.find("body")
        )
        if main is None:
            raise PermanentFailure("listing_parse_error:content_area_missing", url)
        pdf_links = main.find_all("a", href=re.compile(r'\.pdf', re.I))
        if not pdf_links:
            raise PermanentFailure("listing_parse_error:no_pdf_links", url)
        rows, seen_years, seen_urls = [], set(), set()
        for anchor in pdf_links:
            href = anchor.get("href", "").strip()
            if not href:
                continue
            if href.startswith("/"):
                pdf_url = self.base_url + href
            elif href.startswith("http"):
                pdf_url = href
            else:
                pdf_url = self.base_url + "/" + href
            if pdf_url in seen_urls:
                continue
            seen_urls.add(pdf_url)
            year_m = re.search(r'(\d{4})', href)
            year = year_m.group(1) if year_m else "unknown"
            if not year.isdigit() or year in seen_years:
                raise PermanentFailure("listing_parse_error:reporting_year_not_unique", url)
            seen_years.add(year)
            link_text = anchor.get_text(strip=True)
            if not link_text:
                link_text = f"ARN Årsredovisning {year}"
            rows.append({"external_id": year, "year": year, "href": href, "pdf_url": pdf_url,
                         "title": link_text.strip(), "preamble": preamble})
        return rows

    def _enumeration_paper(self, item):
        """Pure copy of crawl()'s Paper conversion for one listing row (None = skipped)."""
        year = item["year"]
        href = item["href"]
        pdf_url = item["pdf_url"]
        title = item["title"]
        original_filename = href.rstrip("/").split("/")[-1]
        published_date = None
        external_id = year
        post_number = year if year.isdigit() else None
        abstract = self._build_abstract(year, item["preamble"])
        if len(abstract) < 50:
            return None
        return {
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": published_date,
            "url": self._LIST_URL,
            "pdf_url": pdf_url,
            "category": "Årsredovisning",
            "publisher": self._PUBLISHER,
            "authors": None,
            "department": None,
            "journal": None,
            "keywords": "årsredovisning,annual report,ARN",
            "doi": None,
            "original_filename": original_filename,
            "metadata": json.dumps(
                {
                    "year": year,
                    "reporting_period_year_raw": year,
                    "year_role": "reporting_period",
                    "published_date_source": None,
                    "original_filename": original_filename,
                    "posted_date": published_date,
                    "source_page": self._LIST_URL,
                },
                ensure_ascii=False,
            ),
        }

    def crawl(self, limit=None):
        """Crawl the årsredovisning listing page and save annual report entries.

        All items are on a single page — no pagination loop needed.
        Returns count of saved documents.
        """
        self._check_cancelled()
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

        print(f"[{self.site_id}] Fetching: {self._LIST_URL}")
        raw = self._curl_get(self._LIST_URL)
        if not raw:
            self._report_source_failure("fetch_failed")
            print(f"[{self.site_id}] ERROR: failed to fetch list page")
            return 0

        try:
            soup = self._make_soup(raw)
        except Exception as exc:
            self._report_source_failure("invalid_response")
            print(f"[{self.site_id}] ERROR: HTML parse failed: {exc}")
            return 0

        if soup is None:
            self._report_source_failure("invalid_response")
            print(f"[{self.site_id}] ERROR: all parsers failed")
            return 0

        # Extract page preamble for use in abstracts
        preamble = ""
        preamble_tag = soup.find("p", class_="preamble")
        if preamble_tag:
            preamble = preamble_tag.get_text(strip=True)
        if not preamble:
            preamble = (
                "Årsredovisningen innehåller en resultatredovisning samt en "
                "ekonomisk redovisning av ARN:s verksamhet."
            )

        # Find all PDF links in the content area
        main = (
            soup.find("main")
            or soup.find("div", {"id": "main-content"})
            or soup.find("div", {"id": "content"})
            or soup.find("body")
        )
        if main is None:
            self._report_source_failure("invalid_response")
            print(f"[{self.site_id}] ERROR: cannot find content area")
            return 0

        pdf_links = main.find_all("a", href=re.compile(r'\.pdf', re.I))
        print(f"[{self.site_id}] Found {len(pdf_links)} PDF links")

        if not pdf_links:
            self._report_source_failure("invalid_response")
            return 0

        saved = 0
        seen_years = set()
        seen_urls: set = set()
        limit_label = str(limit) if limit is not None else "∞"

        for idx, anchor in enumerate(pdf_links):
            self._check_cancelled()
            if limit is not None and saved >= limit:
                break

            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached — stopping at {saved} saved")
                break

            try:
                href = anchor.get("href", "").strip()
                if not href:
                    continue

                # Build absolute PDF URL
                if href.startswith("/"):
                    pdf_url = self.base_url + href
                elif href.startswith("http"):
                    pdf_url = href
                else:
                    pdf_url = self.base_url + "/" + href

                # Deduplicate
                if pdf_url in seen_urls:
                    print(f"[{self.site_id}] item {idx}: duplicate, skipping: {pdf_url}")
                    continue
                seen_urls.add(pdf_url)

                # Extract year from filename or href
                year_m = re.search(r'(\d{4})', href)
                year = year_m.group(1) if year_m else "unknown"
                if not year.isdigit() or year in seen_years:
                    self._report_source_failure("invalid_response")
                    return saved
                seen_years.add(year)

                # Link text → title
                link_text = anchor.get_text(strip=True)
                if not link_text:
                    link_text = f"ARN Årsredovisning {year}"

                # Strip trailing parenthetical "(öppnas i nytt fönster)" from sibling text
                title = link_text.strip()

                # original filename = last path segment
                original_filename = href.rstrip("/").split("/")[-1]

                # The filename year is the reporting period, not a publication
                # or listing date. The source list supplies neither date.
                published_date = None

                # external_id and post_number: the year is the natural key
                external_id = year
                post_number = year if year.isdigit() else None

                abstract = self._build_abstract(year, preamble)

                if len(abstract) < 50:
                    print(f"[{self.site_id}] item {idx}: abstract too short ({len(abstract)} chars), skipping")
                    continue

                paper = {
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "url": self._LIST_URL,
                    "pdf_url": pdf_url,
                    "category": "Årsredovisning",
                    "publisher": self._PUBLISHER,
                    "authors": None,
                    "department": None,
                    "journal": None,
                    "keywords": "årsredovisning,annual report,ARN",
                    "doi": None,
                    "original_filename": original_filename,
                    "metadata": json.dumps(
                        {
                            "year": year,
                            "reporting_period_year_raw": year,
                            "year_role": "reporting_period",
                            "published_date_source": None,
                            "original_filename": original_filename,
                            "posted_date": published_date,
                            "source_page": self._LIST_URL,
                        },
                        ensure_ascii=False,
                    ),
                }

                self._save_paper(paper)
                if self._last_save_created:
                    saved += 1

                if saved % 10 == 0:
                    print(f"[{self.site_id}] page 1: saved {saved}/{limit_label}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                self._report_source_failure("item_failed")
                print(f"[{self.site_id}] item {idx} failed: {exc}")
                continue

        print(f"[{self.site_id}] Done. Saved {saved} items.")
        return saved
