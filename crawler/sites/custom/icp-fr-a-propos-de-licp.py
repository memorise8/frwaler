# -*- coding: utf-8 -*-
"""Crawler for ICP communiqués de presse (Institut Catholique de Paris)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urlparse

from crawler.base_crawler import BaseCrawler

_FRENCH_MONTHS = {
    'janvier': '01', 'février': '02', 'fevrier': '02', 'mars': '03',
    'avril': '04', 'mai': '05', 'juin': '06', 'juillet': '07',
    'août': '08', 'aout': '08', 'septembre': '09', 'octobre': '10',
    'novembre': '11', 'décembre': '12', 'decembre': '12',
}


class ICPFrAPropsDeLICPCrawler(BaseCrawler):
    site_id = "icp-fr-a-propos-de-licp"
    site_name = "Custom: icp-fr-a-propos-de-licp"
    base_url = "https://www.icp.fr"

    LIST_URL = "https://www.icp.fr/a-propos-de-licp/presse/communiques-de-presse"
    MIN_ABSTRACT_CHARS = 50
    MAX_WALL_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute budget
    PAGE_CAP = 200               # safety cap (site has one page; kept for robustness)

    def __init__(self, db_conn, delay=1.0):
        super().__init__(db_conn=db_conn, delay=delay)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else float('inf')
        start_time = time.time()

        # The site delivers all press releases on a single HTML page.
        # We iterate only once (page=1) but keep the pagination scaffold for safety.
        for page_num in range(1, self.PAGE_CAP + 1):
            if time.time() - start_time > self.MAX_WALL_SECONDS:
                print(f"[{self.site_id}] wall-clock budget reached; stopping after page {page_num-1}")
                break

            raw = self._curl_get(self.LIST_URL)
            if not raw:
                print(f"[{self.site_id}] could not fetch list page; stopping")
                break

            soup = self._make_soup(raw, context="list page")
            if soup is None:
                print(f"[{self.site_id}] could not parse list page; stopping")
                break

            records = self._parse_list(soup)
            if not records:
                print(f"[{self.site_id}] page {page_num}: no records found; stopping")
                break

            new_on_page = 0
            for idx, record in enumerate(records, start=1):
                if limit is not None and saved >= limit:
                    break

                if time.time() - start_time > self.MAX_WALL_SECONDS:
                    print(f"[{self.site_id}] wall-clock budget reached inside item loop")
                    break

                pdf_url = record.get('pdf_url', '')
                if not pdf_url or pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    abstract, original_filename = self._extract_pdf(pdf_url)
                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] item {idx} abstract too short "
                            f"({len(abstract)} chars); skipping"
                        )
                        continue

                    if original_filename:
                        record['original_filename'] = original_filename

                    record['abstract'] = abstract[:4000]
                    self._save_paper(record)
                    saved += 1

                    if saved % 10 == 0:
                        print(
                            f"[{self.site_id}] page {page_num}: "
                            f"saved {saved}/{limit_or_inf}"
                        )

                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {idx} failed: {exc}")
                    continue

            # This site has a single page; stop after processing it.
            # new_on_page == 0 would indicate end-of-content or all duplicates.
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: no new records; stopping")
                break

            # Single-page site: always break after the first page.
            break

        print(f"[{self.site_id}] crawl complete: {saved} saved")
        return saved

    # ------------------------------------------------------------------
    # List parsing
    # ------------------------------------------------------------------

    def _parse_list(self, soup) -> list[dict]:
        records: list[dict] = []

        # The press releases live in: div.paragraphe__contenu--0 > ul > li > a
        toolbox = soup.find('div', class_='paragraphe__contenu--0')
        if toolbox is None:
            # Fallback: any div with class containing 'toolbox'
            toolbox = soup.find('div', class_=lambda c: c and 'toolbox' in c)
        if toolbox is None:
            print(f"[{self.site_id}] WARNING: content container not found")
            return records

        for li in toolbox.find_all('li'):
            a_tag = li.find('a')
            if not a_tag:
                continue

            href = a_tag.get('href', '')
            if not href or 'fichier' not in href:
                continue

            # Normalise to absolute URL
            if href.startswith('/'):
                href = self.base_url + href

            # Title: prefer the title attribute (strip size suffix like "- 285 Ko, PDF")
            title = (a_tag.get('title') or a_tag.get_text(strip=True) or '').strip()
            title = re.sub(r'\s*[-–]\s*\d+[,.]?\d*\s*[KMGkmg][Oo],?\s*PDF\s*$', '', title).strip()
            if not title:
                continue

            # Date from the li text (e.g. "Mai 2026 : titre...")
            li_text = li.get_text(strip=True)
            date_str = self._parse_french_date(li_text)

            # External ID and post_number from URL timestamp
            path_seg = urlparse(href).path.rstrip('/').split('/')[-1]  # e.g. "slug_1778658614788-pdf"
            m = re.search(r'_(\d+)(?:-pdf)?$', path_seg)
            ext_id = m.group(1) if m else path_seg
            post_number = ext_id if re.match(r'^\d+$', ext_id) else None

            # Provisional filename (updated later from Content-Disposition)
            provisional_filename = path_seg + '.pdf' if not path_seg.endswith('.pdf') else path_seg

            records.append({
                'external_id': ext_id,
                'post_number': post_number,
                'title': title,
                'url': self.LIST_URL,       # no separate HTML detail page
                'pdf_url': href,
                'published_date': date_str,
                'posted_date': date_str,
                'listed_date': date_str,
                'publisher': 'Institut Catholique de Paris',
                'category': 'Communiqué de presse',
                'original_filename': provisional_filename,
                'metadata': json.dumps({
                    'posted_date': date_str,
                    'li_text': li_text,
                    'slug': path_seg,
                }, ensure_ascii=False),
            })

        return records

    # ------------------------------------------------------------------
    # PDF fetch + text extraction
    # ------------------------------------------------------------------

    def _extract_pdf(self, pdf_url: str) -> tuple[str, str | None]:
        """Download PDF, extract text, return (text, real_filename_or_None)."""
        pdf_bytes = None
        real_filename: str | None = None

        backoff = [1, 3, 9]
        for attempt in range(3):
            try:
                resp = self._session.get(pdf_url, timeout=45, stream=False)
                resp.raise_for_status()
                pdf_bytes = resp.content
                # Real filename from Content-Disposition
                cd = resp.headers.get('content-disposition', '')
                m = re.search(r'filename="?([^";]+)"?', cd)
                if m:
                    real_filename = m.group(1).strip()
                break
            except Exception as exc:
                wait = backoff[attempt]
                print(f"[{self.site_id}] PDF fetch attempt {attempt+1} failed: {exc}")
                if attempt < 2:
                    time.sleep(wait)

        if not pdf_bytes:
            return '', real_filename

        text = self._pdftotext(pdf_bytes)
        if not text:
            text = self._pdfminer_extract(pdf_bytes)

        return text, real_filename

    def _pdftotext(self, pdf_bytes: bytes) -> str:
        tmp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
                tmp.write(pdf_bytes)
                tmp_path = tmp.name
            result = subprocess.run(
                ['pdftotext', tmp_path, '-'],
                capture_output=True, timeout=30,
            )
            return result.stdout.decode('utf-8', errors='replace').strip()
        except Exception as exc:
            print(f"[{self.site_id}] pdftotext error: {exc}")
            return ''
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    def _pdfminer_extract(self, pdf_bytes: bytes) -> str:
        try:
            from io import BytesIO
            from pdfminer.high_level import extract_text
            text = extract_text(BytesIO(pdf_bytes))
            return (text or '').strip()
        except Exception as exc:
            print(f"[{self.site_id}] pdfminer error: {exc}")
            return ''

    # ------------------------------------------------------------------
    # HTML fetch helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, retries: int = 3) -> str | None:
        """Fetch HTML page via curl (TLS-safe, ignores cert errors)."""
        backoff = [1, 3, 9]
        for attempt in range(retries):
            if attempt > 0:
                time.sleep(backoff[attempt - 1])
            try:
                result = subprocess.run(
                    [
                        'curl', '--tls-max', '1.3', '-sk', '-L',
                        '-A', self.USER_AGENT,
                        '--max-time', '45',
                        url,
                    ],
                    capture_output=True, timeout=50,
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode('utf-8', errors='replace')
                print(f"[{self.site_id}] curl rc={result.returncode} for {url}")
            except Exception as exc:
                print(f"[{self.site_id}] curl attempt {attempt+1} failed: {exc}")
        print(f"[{self.site_id}] all {retries} curl attempts failed for {url}")
        return None

    def _make_soup(self, html: str, context: str = ''):
        """Parse HTML, trying parsers in order: html5lib → lxml → html.parser."""
        from bs4 import BeautifulSoup
        for parser in ('html5lib', 'lxml', 'html.parser'):
            try:
                return BeautifulSoup(html, parser)
            except Exception as exc:
                print(f"[{self.site_id}] parser '{parser}' failed ({context}): {exc}")
        return None

    # ------------------------------------------------------------------
    # Date parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_french_date(text: str) -> str | None:
        """Parse French date like 'Mai 2026 : …' → '2026-05-01'."""
        text_lower = text.lower()
        for month_fr, month_num in _FRENCH_MONTHS.items():
            if month_fr in text_lower:
                m = re.search(r'\b(20\d{2})\b', text)
                if m:
                    return f"{m.group(1)}-{month_num}-01"
        # Year-only fallback
        m = re.search(r'\b(20\d{2})\b', text)
        if m:
            return f"{m.group(1)}-01-01"
        return None
