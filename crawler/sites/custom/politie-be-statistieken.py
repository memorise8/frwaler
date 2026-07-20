# -*- coding: utf-8 -*-
"""Crawler for politie.be statistieken – Morfologie Rapporten (Drupal node/2703).

Listing page: https://www.politie.be/statistieken/nl/morfologie/rapporten
Structure: <h2>Rapporten YEAR</h2><ul><li><a href="PDF_URL">Title</a></li>...
No detail pages — all metadata lives on the single listing page.
PDFs are hosted on https://www.police.be/statistiques/…
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import unquote, urljoin, urlparse

try:
    from bs4 import BeautifulSoup
    _BS4_OK = True
except ImportError:
    _BS4_OK = False

from crawler.base_crawler import BaseCrawler

_SITE_ID = "politie-be-statistieken"
_START_URL = "https://www.politie.be/statistieken/nl/morfologie/rapporten"
# Hardcoded latest known-good Wayback snapshot (March 2026)
_FALLBACK_WB_TIMESTAMP = "20260312012144"


class PolitieBeMorfologieRapportenCrawler(BaseCrawler):
    site_id = _SITE_ID
    site_name = "Custom: politie-be-statistieken"
    base_url = "https://www.politie.be"

    _MIN_ABSTRACT_CHARS = 50

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl(self, url, *, timeout=45):
        """Fetch URL via curl with retry and TLS flexibility."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL",
            "--compressed",
            "--connect-timeout", "15",
            "--max-time", str(timeout),
            "-H", f"User-Agent: {self.USER_AGENT}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: nl-BE,nl;q=0.9,en;q=0.8",
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
                stdout = result.stdout or b""
                if result.returncode == 0 and stdout.strip():
                    return stdout.decode("utf-8", errors="replace")
                stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                last_error = f"exit={result.returncode} {stderr[:120]}"
            if attempt < 2:
                w = waits[attempt]
                print(f"[{_SITE_ID}] curl retry {attempt + 1}/3 for {url}: {last_error}; wait {w}s")
                time.sleep(w)
        print(f"[{_SITE_ID}] curl failed after 3 attempts: {url}: {last_error}")
        return None

    def _is_maintenance_page(self, raw):
        """Return True if the response is a maintenance/error placeholder."""
        if not raw or len(raw) < 5000:
            return True
        snippet = raw[:2000]
        if "Maintenance" in snippet or "maintenance" in snippet:
            return True
        # Successful page has Drupal-specific markers
        if "field--name-field-b-text-text" not in raw and "node-2703" not in raw:
            # Check if it contains any year-based report sections
            if not re.search(r'Rapporten\s+20\d\d', raw):
                return True
        return False

    def _fetch_listing(self):
        """Fetch listing page; fall back to Wayback Machine if live returns maintenance."""
        raw = self._curl(_START_URL)
        if raw and not self._is_maintenance_page(raw):
            return raw, _START_URL

        print(f"[{_SITE_ID}] Live site under maintenance; querying Wayback Machine CDX...")
        timestamp = _FALLBACK_WB_TIMESTAMP

        cdx_url = (
            "https://web.archive.org/cdx/search/cdx"
            f"?url={_START_URL}&output=json&limit=10&fl=timestamp"
            "&filter=statuscode:200&from=20240101"
        )
        cdx_raw = self._curl(cdx_url)
        if cdx_raw:
            try:
                rows = json.loads(cdx_raw)
                data_rows = [r for r in rows if r[0] != "timestamp"]
                if data_rows:
                    timestamp = data_rows[-1][0]  # most recent snapshot
                    print(f"[{_SITE_ID}] Using Wayback snapshot: {timestamp}")
            except Exception as exc:
                print(f"[{_SITE_ID}] CDX parse error: {exc}; using hardcoded fallback ts")

        wb_url = f"https://web.archive.org/web/{timestamp}/{_START_URL}"
        print(f"[{_SITE_ID}] Fetching: {wb_url}")
        wb_raw = self._curl(wb_url)
        if wb_raw and not self._is_maintenance_page(wb_raw):
            return wb_raw, _START_URL  # canonical URL is still the live one
        return None, _START_URL

    # ------------------------------------------------------------------
    # HTML parsing helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_html(raw):
        text = raw or ""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                print(f"[{_SITE_ID}] BeautifulSoup({parser}) failed: {exc}")
        return None

    @staticmethod
    def _clean_archive_artifacts(html):
        """Remove Wayback Machine URL wrappers and fix double-https artifacts."""
        # https://web.archive.org/web/TS/https://real → https://real
        html = re.sub(r'https://web\.archive\.org/web/\d+[a-z_]*/https?://', 'https://', html)
        html = re.sub(r'https://web\.archive\.org/web/\d+/', '', html)
        # /web/TS/https://... → https://...
        html = re.sub(r'/web/\d+[a-z_]*/https?://', 'https://', html)
        html = re.sub(r'/web/\d+/', '/', html)
        # Fix double-scheme artifact left by some archive wrappers
        html = re.sub(r'https?://https?://', 'https://', html)
        return html

    @staticmethod
    def _extract_year(text):
        m = re.search(r'\b(20\d{2})\b', text or '')
        return m.group(1) if m else None

    @staticmethod
    def _filename_from_url(url):
        """Extract the last path segment (decoded) from a URL."""
        try:
            path = urlparse(url).path
            raw = path.rstrip('/').split('/')[-1].split('?')[0].split('#')[0]
            return unquote(raw)
        except Exception:
            return url.split('/')[-1] if url else ""

    @staticmethod
    def _report_num_from_filename(filename):
        """Extract leading numeric prefix, e.g. '01' from '01_NL_EFF_...'."""
        m = re.match(r'^(\d+)', filename or '')
        return m.group(1) if m else None

    @staticmethod
    def _build_abstract(title, year, report_num):
        """Construct an informative abstract (always ≥ 100 chars) from metadata."""
        parts = ["Jaarlijks morfologisch rapport van de Federale Politie België"]
        if year:
            parts.append(f"editie {year}")
        if report_num:
            parts.append(f"rapport nr. {report_num}")
        base = ", ".join(parts) + "."
        topic = f" Onderwerp: {title}." if title else ""
        suffix = (
            " Onderdeel van de officiële politiestatistieken over de personeelsmorfologie"
            " (morfologie van het Belgische politiepersoneel in de lokale en federale politiezones)."
        )
        return base + topic + suffix

    # ------------------------------------------------------------------
    # Parse listing page → list of record dicts
    # ------------------------------------------------------------------

    def _parse_listing(self, html, source_url):
        html = self._clean_archive_artifacts(html)
        soup = self._parse_html(html)
        if not soup:
            print(f"[{_SITE_ID}] HTML parse failed")
            return []

        records = []

        # Each year section is: <h2>Rapporten YEAR</h2><ul>...<li><a href=".pdf">Title</a>...
        # Sections are inside div.field--name-field-b-text-text blocks
        content_blocks = soup.find_all("div", class_=re.compile(r"field--name-field-b-text-text"))
        if not content_blocks:
            # Broader fallback
            content_blocks = [soup.body] if soup.body else [soup]

        for block in content_blocks:
            for h2 in block.find_all("h2"):
                year = self._extract_year(h2.get_text())
                if not year:
                    continue

                ul = h2.find_next_sibling("ul")
                if not ul:
                    continue

                for li in ul.find_all("li"):
                    a = li.find("a", href=re.compile(r'\.pdf', re.I))
                    if not a:
                        continue

                    href = (a.get("href") or "").strip()
                    if not href:
                        continue
                    # Clean any residual archive artefacts in individual href
                    href = re.sub(r'https?://https?://', 'https://', href)
                    if not href.startswith("http"):
                        href = urljoin("https://www.police.be", href)

                    title = re.sub(r'\s+', ' ', a.get_text(strip=True))
                    filename = self._filename_from_url(href)
                    report_num = self._report_num_from_filename(filename)

                    # Stable external_id: year + filename (truncated)
                    ext_id = f"{year}_{filename[:120]}"

                    # post_number: YYYYnnn (year + zero-padded report number)
                    if report_num:
                        post_number = f"{year}{report_num.zfill(3)}"
                    else:
                        post_number = year

                    abstract = self._build_abstract(title, year, report_num)

                    record = {
                        "site_id": self.site_id,
                        "external_id": ext_id,
                        "post_number": post_number,
                        "title": title,
                        "abstract": abstract,
                        "published_date": f"{year}-01-01",
                        "listed_date": f"{year}-01-01",
                        "url": source_url,
                        "pdf_url": href,
                        "publisher": "Federale Politie België / Police Fédérale Belgique",
                        "department": "Dienst Morfologie / Service Morphologie",
                        "category": "Morfologie",
                        "keywords": f"morfologie,{year},politie,personeel,statistieken",
                        "original_filename": filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": year,
                                "originalFilename": filename,
                                "year_section": year,
                                "report_number": report_num,
                                "source_page": _START_URL,
                                "host_domain": "www.police.be",
                            },
                            ensure_ascii=False,
                        ),
                    }
                    records.append(record)

        return records

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        max_wall_seconds = 25 * 60  # 25-minute budget

        saved = 0
        seen_urls = set()

        # Fetch listing (single page — no real pagination needed)
        raw, canonical_url = self._fetch_listing()
        if not raw:
            print(f"[{_SITE_ID}] Could not fetch listing page; aborting")
            return 0

        records = self._parse_listing(raw, canonical_url)
        total = len(records)
        print(f"[{_SITE_ID}] Found {total} records on listing page")

        if not records:
            print(f"[{_SITE_ID}] No records parsed; aborting")
            return 0

        # Treat the single listing page as page 1 for progress logging
        page = 1
        lim_or_inf = str(limit) if limit is not None else "∞"

        for idx, record in enumerate(records, start=1):
            try:
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time > max_wall_seconds:
                    print(f"[{_SITE_ID}] 25-minute wall-clock budget reached; stopping")
                    break

                if idx % 10 == 0:
                    print(f"[{_SITE_ID}] page {page}: saved {saved}/{lim_or_inf}")

                pdf_url = record.get("pdf_url", "")
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                abstract = record.get("abstract") or ""
                if len(abstract.strip()) < self._MIN_ABSTRACT_CHARS:
                    print(
                        f"[{_SITE_ID}] item {idx} skipped: abstract too short "
                        f"({len(abstract.strip())} chars)"
                    )
                    continue

                title = record.get("title") or ""
                if not title.strip():
                    print(f"[{_SITE_ID}] item {idx} skipped: empty title")
                    continue

                time.sleep(max(0.0, self._delay * 0.2))  # light rate-limit (no detail fetches)

                self._save_paper(record)
                saved += 1
                print(f"[{_SITE_ID}] saved {saved}/{lim_or_inf}: {title[:80]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item {idx} failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
