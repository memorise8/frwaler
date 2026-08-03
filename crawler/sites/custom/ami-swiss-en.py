# -*- coding: utf-8 -*-
"""Adolphe Merkle Institute (ami.swiss) English publications crawler.

Starting URL: https://www.ami.swiss/en/research/publications.html
Publications are listed by year; abstracts are fetched from CrossRef API.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import time

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _strip_jats(text: str) -> str:
    """Remove JATS/XML tags and normalize whitespace."""
    text = re.sub(r"</?jats:[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


class AmiSwissEnCrawler(BaseCrawler):
    """Crawler for AMI Swiss publications (ami.swiss)."""

    site_id = "ami-swiss-en"
    site_name = "Custom: ami-swiss-en"
    base_url = "https://www.ami.swiss"

    _LIST_URL = "https://www.ami.swiss/en/research/publications.html"
    _CROSSREF_BASE = "https://api.crossref.org/works"
    _MIN_ABSTRACT = 100
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

    # ------------------------------------------------------------------
    # curl helper
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _get_years(self) -> list:
        """Return list of publication years (newest first) from main page."""
        raw = self._curl_get(self._LIST_URL)
        if not raw:
            return []
        years = sorted(set(re.findall(r'name="year" value="(\d{4})"', raw)), reverse=True)
        return years

    def _parse_year_page(self, html: str, year: str) -> list:
        """Parse all unique publication entries from a year page."""
        entries = []
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for year {year}: {exc}")
            return entries

        article = soup.find("article", class_="bg-white")
        container = article if article else soup

        seen_dois: set = set()
        for box in container.find_all("div", class_=re.compile(r"\bbox\b")):
            try:
                # Main link must point to a DOI
                link = box.find("a", href=re.compile(r"doi\.org"))
                if not link:
                    continue
                href = (link.get("href") or "").strip()
                doi_match = re.search(r"doi\.org/(.+)", href)
                if not doi_match:
                    continue
                doi = doi_match.group(1).strip()
                if doi in seen_dois:
                    continue
                seen_dois.add(doi)

                spans = link.find_all("span")
                title = spans[0].get_text(strip=True) if spans else ""
                authors_raw = spans[1].get_text(strip=True) if len(spans) > 1 else ""
                journal_span = spans[2] if len(spans) > 2 else None

                journal = ""
                volume = ""
                if journal_span:
                    b_tag = journal_span.find("b")
                    journal = b_tag.get_text(strip=True) if b_tag else ""
                    j_text = journal_span.get_text(" ", strip=True)
                    # Strip journal name and year to isolate volume digits
                    stripped = j_text.replace(journal, "").replace(year, "")
                    vol_match = re.search(r"\b(\d{1,4})\b", stripped)
                    volume = vol_match.group(1) if vol_match else ""

                entries.append({
                    "title": title,
                    "authors_raw": authors_raw,
                    "journal": journal,
                    "volume": volume,
                    "doi": doi,
                    "url": href,
                    "year": year,
                })
            except Exception as exc:
                print(f"[{self.site_id}] box parse error (year {year}): {exc}")
                continue

        return entries

    def _fetch_crossref(self, doi: str) -> dict:
        """Fetch CrossRef metadata for a DOI. Returns empty dict on failure."""
        url = f"{self._CROSSREF_BASE}/{doi}"
        raw = self._curl_get(url)
        if not raw:
            return {}
        try:
            return json.loads(raw).get("message", {})
        except (json.JSONDecodeError, AttributeError, ValueError):
            return {}

    @staticmethod
    def _crossref_pdf_url(msg: dict) -> str:
        """Extract a direct PDF link from CrossRef's link metadata, if any."""
        for link in msg.get("link") or []:
            if link.get("content-type") == "application/pdf" and link.get("URL"):
                return link["URL"]
        return ""

    @staticmethod
    def _crossref_date(msg: dict) -> str:
        """Extract best ISO date from CrossRef message dict."""
        for key in ("published", "published-print", "published-online", "issued", "created"):
            dp = (msg.get(key) or {}).get("date-parts", [[]])
            if dp and dp[0]:
                parts = dp[0]
                if len(parts) >= 3:
                    return f"{parts[0]:04d}-{int(parts[1]):02d}-{int(parts[2]):02d}"
                if len(parts) == 2:
                    return f"{parts[0]:04d}-{int(parts[1]):02d}-01"
                if len(parts) == 1:
                    return f"{parts[0]:04d}-01-01"
        return ""

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl AMI Swiss publications year by year, enriching abstracts via CrossRef."""
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page_num = 0

        years = self._get_years()
        if not years:
            print(f"[{self.site_id}] No years found. Aborting.")
            return 0

        print(f"[{self.site_id}] Found {len(years)} years: {years[0]}–{years[-1]}")

        try:
            for year in years:
                if limit is not None and saved >= limit:
                    break
                if page_num >= self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_num += 1
                year_url = f"{self._LIST_URL}?year={year}"
                raw = self._curl_get(year_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch year {year}. Skipping.")
                    continue

                entries = self._parse_year_page(raw, year)
                if not entries:
                    print(f"[{self.site_id}] No entries found for year {year}.")
                    continue

                for entry in entries:
                    if limit is not None and saved >= limit:
                        break

                    url = entry["url"]
                    if url in seen_urls:
                        continue
                    seen_urls.add(url)

                    try:
                        doi = entry["doi"]
                        title = entry["title"]
                        if not title:
                            print(f"[{self.site_id}] Empty title for DOI {doi}, skipping.")
                            continue

                        # Fetch CrossRef for abstract + structured metadata
                        time.sleep(self._delay)
                        cf = self._fetch_crossref(doi) if doi else {}

                        abstract = _strip_jats(cf.get("abstract") or "")
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] Short abstract ({len(abstract)}) for '{title[:50]}', skipping.")
                            continue

                        # Dates
                        published_date = self._crossref_date(cf) or f"{entry['year']}-01-01"
                        listed_date = f"{entry['year']}-01-01"

                        # Authors: prefer CrossRef structured list
                        cf_authors = cf.get("author") or []
                        if cf_authors:
                            author_parts = []
                            for a in cf_authors:
                                name = " ".join(
                                    p for p in [a.get("given", ""), a.get("family", "")] if p
                                )
                                if name:
                                    author_parts.append(name)
                            authors = "; ".join(author_parts)
                        else:
                            # Fall back to comma-separated page data
                            authors = "; ".join(
                                p.strip() for p in entry["authors_raw"].split(",") if p.strip()
                            )

                        # Journal
                        cf_titles = cf.get("container-title") or []
                        journal = html.unescape(cf_titles[0]) if cf_titles else entry["journal"]

                        # Publisher / volume / issue / subjects
                        publisher = cf.get("publisher") or ""
                        volume = cf.get("volume") or entry.get("volume") or ""
                        issue = cf.get("issue") or ""
                        subjects = cf.get("subject") or []
                        keywords = ", ".join(subjects) if subjects else ""
                        pdf_url = self._crossref_pdf_url(cf) or None

                        meta = {
                            "posted_date": listed_date,
                            "journal_raw": entry["journal"],
                            "crossref_type": cf.get("type"),
                        }
                        if volume:
                            meta["volume"] = volume
                        if issue:
                            meta["issue"] = issue

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": doi,
                            "post_number": doi,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": listed_date,
                            "authors": authors,
                            "publisher": publisher,
                            "journal": journal,
                            "url": url,
                            "pdf_url": pdf_url,
                            "keywords": keywords,
                            "category": "",
                            "doi": doi,
                            "original_filename": None,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(
                            f"[{self.site_id}] item failed "
                            f"(doi={entry.get('doi', '?')}): {exc}; continuing."
                        )
                        continue

                if page_num % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
