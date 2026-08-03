# -*- coding: utf-8 -*-
"""BMWKMS Innovative Film Austria publications crawler.

Starting URL:
  https://www.bmwkms.gv.at/themen/kunst-und-kultur/service-kunst-und-kultur/
  publikationen/berichte-innovative-film-austria.html

Single HTML listing page with annual PDF catalogs (05/06 – 21/22).
No JSON API, no pagination — all entries are on one page.
"""

import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote

# Absolute import — required because spec_from_file_location has no package context.
sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler

_SITE_ID = "bmwkms-gv-at-themen"
_BASE = "https://www.bmwkms.gv.at"
_START_URL = (
    "https://www.bmwkms.gv.at/themen/kunst-und-kultur/service-kunst-und-kultur"
    "/publikationen/berichte-innovative-film-austria.html"
)
_PUBLISHER = (
    "Bundesministerium für Wohnen, Kunst, Kultur, Medien und Sport (BMWKMS)"
)
# General description of the publication series extracted from the page.
_SERIES_DESC = (
    "Der künstlerische Erfolg der von der Filmabteilung geförderten Filme wird seit "
    "dem Jahr 2005 in jährlichen Katalogen dokumentiert. Neben Informationen zu den "
    "Filmen selbst finden sich darin auch Angaben zu Festivaleinladungen und Preisen. "
    "Herausgeber: Bundesministerium für Wohnen, Kunst, Kultur, Medien und Sport "
    "(BMWKMS), Sektion IV – Kunst und Kultur, Concordiaplatz 2, 1010 Wien."
)
_MAX_WALL_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Parse HTML with fallback chain: html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _curl_get(url: str, retries: int = 3):
    """Fetch URL via curl with TLS 1.3 and exponential backoff (1 s, 3 s, 9 s)."""
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
        "-H", (
            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
        "-H", "Accept: text/html,*/*;q=0.8",
        "-H", "Accept-Language: de-AT,de;q=0.9,en;q=0.8",
        url,
    ]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=35)
            raw = result.stdout.decode("utf-8", errors="replace")
            if raw.strip():
                return raw
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] empty response for {url}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = 1 * (3 ** attempt)
                print(f"[{_SITE_ID}] curl error: {exc}, "
                      f"retry {attempt + 1}/{retries} in {wait}s")
                time.sleep(wait)
    return None


def _year_from_title(title: str):
    """Extract post_number and ISO date from a title like 'Innovative Film Austria 21/22'.

    Returns (post_number_str, iso_date_str) or (None, None).
    """
    m = re.search(r'(\d{2})/(\d{2})', title)
    if m:
        yy1, yy2 = int(m.group(1)), int(m.group(2))
        year = 2000 + yy2 if yy2 <= 50 else 1900 + yy2
        return f"{yy1:02d}{yy2:02d}", f"{year}-01-01"
    m4 = re.search(r'(20\d{2})', title)
    if m4:
        return m4.group(1), f"{m4.group(1)}-01-01"
    return None, None


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class BmwkmsGvAtThemenCrawler(BaseCrawler):
    """Crawler for BMWKMS Innovative Film Austria annual PDF reports."""

    site_id = "bmwkms-gv-at-themen"
    site_name = "Custom: bmwkms-gv-at-themen"
    base_url = "https://www.bmwkms.gv.at"

    def crawl(self, limit=None):
        """Fetch the listing page and save one record per PDF entry.

        Parameters
        ----------
        limit:
            Maximum number of records to save.  None = unlimited.
        """
        t0 = time.time()
        saved = 0
        seen_urls: set = set()

        # ----------------------------------------------------------------
        # Fetch and parse the single listing page
        # ----------------------------------------------------------------
        raw = _curl_get(_START_URL)
        if not raw:
            print(f"[{_SITE_ID}] Failed to fetch listing page")
            return 0

        soup = _make_soup(raw)
        if soup is None:
            print(f"[{_SITE_ID}] HTML parse returned None")
            return 0

        file_links = soup.find_all("a", class_="file")
        if not file_links:
            print(f"[{_SITE_ID}] No <a class=\"file\"> links found on page")
            return 0

        print(f"[{_SITE_ID}] Found {len(file_links)} entries on listing page")

        # ----------------------------------------------------------------
        # Process each PDF entry
        # ----------------------------------------------------------------
        for link in file_links:
            if limit is not None and saved >= limit:
                break
            if time.time() - t0 > _MAX_WALL_S:
                print(f"[{_SITE_ID}] Wall-clock budget (25 min) reached, stopping")
                break

            try:
                href = (link.get("href") or "").strip()
                if not href:
                    continue

                # Absolute PDF URL
                if href.startswith("/"):
                    pdf_url = _BASE + href
                elif href.startswith("http"):
                    pdf_url = href
                else:
                    pdf_url = _BASE + "/" + href

                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)

                # Title — strip the file-info "(PDF, X MB)" suffix
                raw_text = link.get_text(separator=" ", strip=True)
                title = re.sub(r'\s*\(PDF[^)]*\)', '', raw_text).strip()
                title = re.sub(r'\s+', ' ', title).strip()
                if not title:
                    title = "Innovative Film Austria"

                # Original filename from the URL path
                url_path = href.split("?")[0]
                original_filename = unquote(url_path.split("/")[-1])

                # external_id: JCR UUID embedded in the DAM path
                jcr_match = re.search(r'/dam/jcr:([^/]+)/', href)
                external_id = (
                    jcr_match.group(1) if jcr_match else original_filename
                )

                # Year-based post_number and publication date
                post_number, published_date = _year_from_title(title)

                # Abstract: title + series description (always >> 100 chars)
                abstract = f"{title} — {_SERIES_DESC}"

                if len(abstract) < 50:
                    print(f"[{_SITE_ID}] abstract too short for '{title}', skipping")
                    continue

                # File-size annotation from <span class="fileinfo">
                size_span = link.find("span", class_="fileinfo")
                file_size = size_span.get_text(strip=True) if size_span else None

                self._save_paper({
                    "site_id": self.site_id,
                    "external_id": external_id,
                    "post_number": post_number,
                    "url": _START_URL,
                    "title": title,
                    "abstract": abstract,
                    "published_date": published_date,
                    "posted_date": published_date,
                    "authors": None,
                    "publisher": _PUBLISHER,
                    "department": "Sektion IV – Kunst und Kultur",
                    "journal": None,
                    "pdf_url": pdf_url,
                    "original_filename": original_filename,
                    "keywords": (
                        "Innovative Film Austria,Filmabteilung,"
                        "Österreichischer Film,Festivaleinladungen,Kulturförderung"
                    ),
                    "category": "Innovative Film Austria",
                    "doi": None,
                    "metadata": json.dumps({
                        "posted_date": published_date,
                        "originalFilename": original_filename,
                        "jcr_uuid": jcr_match.group(1) if jcr_match else None,
                        "source_page": _START_URL,
                        "file_size": file_size,
                    }, ensure_ascii=False),
                })
                saved += 1
                print(f"[{_SITE_ID}] saved ({saved}): {title[:70]}")

            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"[{_SITE_ID}] item failed: {exc}")
                continue

        print(f"[{_SITE_ID}] Done. Total saved: {saved}")
        return saved
