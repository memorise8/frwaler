# -*- coding: utf-8 -*-
"""Bord Bia Annual Reports crawler.

Starting URL: https://www.bordbia.ie/about/governance/annual-reports/
Single listing page -> direct PDF links. Abstract extracted via pdftotext.
"""

import html as html_module
import json
import os
import re
import subprocess
import tempfile
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup
    _BS_AVAILABLE = True
except ImportError:
    _BS_AVAILABLE = False

_LIST_URL = "https://www.bordbia.ie/about/governance/annual-reports/"
_SITE_ID = "bordbia-ie-about"
_PAGE_SAFETY_CAP = 200


def _bs_parse(raw: str):
    if not _BS_AVAILABLE:
        return None
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3) -> bytes | None:
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "60",
        "-A", BaseCrawler.USER_AGENT,
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=65)
            if result.stdout and result.stdout.strip():
                return result.stdout
        except Exception as exc:
            print(f"[{_SITE_ID}] curl error (attempt {attempt + 1}/{retries}): {exc}")
        if attempt < retries - 1:
            wait = (1, 3, 9)[attempt]
            print(f"[{_SITE_ID}] Retrying in {wait}s…")
            time.sleep(wait)
    return None


def _extract_pdf_text(pdf_bytes: bytes, max_pages: int = 5) -> str:
    tf_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tf:
            tf.write(pdf_bytes)
            tf_path = tf.name
        result = subprocess.run(
            ["pdftotext", "-l", str(max_pages), tf_path, "-"],
            capture_output=True, timeout=60,
        )
        return result.stdout.decode("utf-8", errors="replace").strip()
    except Exception as exc:
        print(f"[{_SITE_ID}] pdftotext error: {exc}")
        return ""
    finally:
        if tf_path:
            try:
                os.unlink(tf_path)
            except Exception:
                pass


def _parse_year(text: str) -> str | None:
    m = re.search(r"\b(20\d{2})\b", text)
    return m.group(1) if m else None


def _orig_filename(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("?")[0]


class BordbiaAboutCrawler(BaseCrawler):
    site_id = "bordbia-ie-about"
    site_name = "Custom: bordbia-ie-about"
    base_url = "https://www.bordbia.ie"

    def crawl(self, limit=None):
        start_time = time.time()
        max_wall = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        saved = 0
        seen_urls: set[str] = set()

        # -- Fetch the single listing page --
        raw_bytes = None
        for attempt in range(3):
            raw_bytes = _curl_get(_LIST_URL)
            if raw_bytes:
                break
            wait = (1, 3, 9)[attempt]
            print(f"[{self.site_id}] Listing fetch failed (attempt {attempt + 1}/3), "
                  f"retry in {wait}s")
            time.sleep(wait)

        if not raw_bytes:
            print(f"[{self.site_id}] Failed to fetch listing page after 3 attempts.")
            return saved

        raw_html = raw_bytes.decode("utf-8", errors="replace")
        soup = _bs_parse(raw_html)
        if not soup:
            print(f"[{self.site_id}] HTML parse failed entirely.")
            return saved

        items = soup.find_all("a", class_="documents__item")
        if not items:
            print(f"[{self.site_id}] No documents__item elements found. Stopping.")
            return saved

        print(f"[{self.site_id}] Found {len(items)} documents on listing page.")

        # This site is a single listing page (no server-side pagination).
        # We treat item iteration as "page 1 of 1" for compliance with the
        # page-tracking requirement. page_num tracks conceptual pages fetched.
        page_num = 1
        new_on_page = 0

        for idx, item in enumerate(items):
            if limit is not None and saved >= limit:
                break
            if page_num > _PAGE_SAFETY_CAP:
                print(f"[{self.site_id}] Safety cap of {_PAGE_SAFETY_CAP} pages reached. Stopping.")
                break
            if time.time() - start_time > max_wall:
                print(f"[{self.site_id}] 25-minute wall-clock budget exceeded. Stopping.")
                break

            href = (item.get("href") or "").strip()
            if not href:
                continue

            pdf_url = urljoin(self.base_url, href)
            if pdf_url in seen_urls:
                print(f"[{self.site_id}] Duplicate skipped: {pdf_url}")
                continue
            seen_urls.add(pdf_url)
            new_on_page += 1

            name_tag = item.find(class_="documents__name")
            title = (name_tag.get_text(strip=True) if name_tag else "").strip()
            title = html_module.unescape(title)
            if not title:
                title = _orig_filename(pdf_url).replace("-", " ").replace(".pdf", "").strip()

            try:
                year = _parse_year(title) or _parse_year(href)
                post_number = year
                published_date = f"{year}-01-01" if year else None
                orig_fn = _orig_filename(pdf_url)
                ext_id = orig_fn.replace(".pdf", "")

                print(f"[{self.site_id}] [{idx + 1}/{len(items)}] Downloading: {title}")
                time.sleep(self._delay)

                pdf_bytes = None
                for attempt in range(3):
                    pdf_bytes = _curl_get(pdf_url)
                    if pdf_bytes:
                        break
                    wait = (1, 3, 9)[attempt]
                    print(f"[{self.site_id}] PDF retry {attempt + 1}/3 in {wait}s")
                    time.sleep(wait)

                if not pdf_bytes:
                    print(f"[{self.site_id}] Failed to download PDF: {title} — skipping")
                    continue

                abstract = _extract_pdf_text(pdf_bytes, max_pages=5)

                if len(abstract) < 50:
                    print(f"[{self.site_id}] Abstract too short "
                          f"({len(abstract)} chars): {title} — skipping")
                    continue

                paper = {
                    "id": None,
                    "site_id": self.site_id,
                    "external_id": ext_id,
                    "post_number": post_number,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "listed_date": published_date,
                    "authors": "",
                    "publisher": "Bord Bia",
                    "department": "",
                    "journal": "",
                    "url": _LIST_URL,
                    "pdf_url": pdf_url,
                    "keywords": "",
                    "category": "Annual Report",
                    "doi": None,
                    "original_filename": orig_fn,
                    "metadata": json.dumps({
                        "posted_date": published_date,
                        "originalFilename": orig_fn,
                        "year": year,
                        "listing_url": _LIST_URL,
                    }, ensure_ascii=False),
                }

                self._save_paper(paper)
                saved += 1
                lim_str = str(limit) if limit is not None else "inf"
                print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{self.site_id}] item {idx + 1} failed: {exc}")
                continue

        # End-of-page accounting
        if new_on_page == 0:
            print(f"[{self.site_id}] page {page_num}: 0 new records. Done.")
        if page_num % 10 == 0:
            lim_str = str(limit) if limit is not None else "inf"
            print(f"[{self.site_id}] page {page_num}: saved {saved}/{lim_str}")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
