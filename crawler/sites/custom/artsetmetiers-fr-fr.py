# -*- coding: utf-8 -*-
"""Arts et Métiers (artsetmetiers.fr) press-release crawler.

Starting URL: https://artsetmetiers.fr/fr/communiques-de-presse
Structure: Drupal 11 view "espace_presse", paginated with ?page=N (0-indexed).
Each views-row contains a direct S3 PDF link, date spans, <h2> title, <p> abstract.
No HTML detail page exists — all data is harvested from the list pages.
"""

import json
import os
import re
import subprocess
import time
import urllib.parse

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# French month abbreviation → zero-padded number
# ---------------------------------------------------------------------------
_MONTH_FR = {
    "jan": "01",
    "fév": "02", "fev": "02",
    "mar": "03",
    "avr": "04",
    "mai": "05",
    "jun": "06", "juin": "06",
    "jul": "07", "juil": "07",
    "aoû": "08", "aou": "08", "aoû": "08",
    "sep": "09",
    "oct": "10",
    "nov": "11",
    "déc": "12", "dec": "12",
}


def _parse_fr_date(day: str, month: str, year: str) -> str | None:
    """Convert French date parts to ISO YYYY-MM-DD."""
    try:
        # Normalise: strip, lower, take first 3 chars, drop accents
        mk = month.strip().lower()[:4]
        mk = mk.replace("é", "e").replace("û", "u").replace("è", "e")
        mk = mk[:3]
        mm = _MONTH_FR.get(mk)
        if not mm:
            return None
        dd = day.strip().zfill(2)
        yyyy = year.strip()
        if len(yyyy) == 4 and dd.isdigit() and mm:
            return f"{yyyy}-{mm}-{dd}"
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# BeautifulSoup factory with parser fallback chain
# ---------------------------------------------------------------------------

def _make_soup(html: str):
    """Return a BeautifulSoup object; try html5lib → lxml → html.parser."""
    from bs4 import BeautifulSoup  # noqa: PLC0415
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class ArtsetmetiersFrFrCrawler(BaseCrawler):
    """Crawler for Arts et Métiers press releases."""

    site_id = "artsetmetiers-fr-fr"
    site_name = "Custom: artsetmetiers-fr-fr"
    base_url = "https://artsetmetiers.fr"

    _LIST_URL = "https://artsetmetiers.fr/fr/communiques-de-presse"
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))       # safety cap
    _WALL_CLOCK_LIMIT = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes in seconds

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_page_html(self, page_num: int) -> str | None:
        """Fetch one list page via curl; returns raw HTML string or None."""
        url = f"{self._LIST_URL}?page={page_num}"
        delays = [1, 3, 9]
        for attempt in range(3):
            if attempt > 0:
                time.sleep(delays[attempt])
            try:
                cmd = [
                    "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
                    "-H", f"User-Agent: {self.USER_AGENT}",
                    "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
                    "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
                    url,
                ]
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35
                )
                if result.returncode == 0 and result.stdout:
                    return result.stdout.decode("utf-8", errors="replace")
                print(
                    f"[{self.site_id}] page {page_num} attempt {attempt + 1}/3:"
                    f" empty/error (rc={result.returncode})"
                )
            except Exception as exc:
                print(
                    f"[{self.site_id}] page {page_num} attempt {attempt + 1}/3:"
                    f" fetch error: {exc}"
                )
        return None

    def _parse_list_page(self, html: str) -> list[dict]:
        """Parse a list-page HTML; return list of raw item dicts."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BeautifulSoup construction failed: {exc}")
            return []
        if soup is None:
            return []

        items = []
        for row in soup.select("div.views-row"):
            try:
                # ---- PDF URL (outer <a href="...s3...">)
                a_tag = row.find("a", href=True)
                if not a_tag:
                    continue
                pdf_url = a_tag.get("href", "").strip()
                if not pdf_url:
                    continue
                if pdf_url.startswith("mailto:") or pdf_url.startswith("#"):
                    continue
                if not pdf_url.startswith("http"):
                    pdf_url = self.base_url + pdf_url

                # ---- Date
                day_tag = row.find("span", class_="day")
                mon_tag = row.find("span", class_="month")
                yr_tag = row.find("span", class_="year")
                day = day_tag.get_text(strip=True) if day_tag else ""
                month = mon_tag.get_text(strip=True) if mon_tag else ""
                year = yr_tag.get_text(strip=True) if yr_tag else ""
                date_str = _parse_fr_date(day, month, year) if (day and month and year) else None

                # ---- Title
                h2 = row.find("h2")
                title = h2.get_text(strip=True) if h2 else ""

                # ---- Abstract
                p = row.find("p")
                abstract = p.get_text(strip=True) if p else ""

                items.append({
                    "pdf_url": pdf_url,
                    "date": date_str,
                    "title": title,
                    "abstract": abstract,
                })
            except Exception as exc:
                print(f"[{self.site_id}] row parse error: {exc}")
                continue

        return items

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl all press-release list pages and persist records.

        Parameters
        ----------
        limit:
            Maximum number of records to save (None = unlimited).
        """
        saved = 0
        seen_urls: set[str] = set()
        start_time = time.time()
        limit_str = str(limit) if limit is not None else "∞"

        for page_num in range(self._MAX_PAGES):
            # ---- Limit guard
            if limit is not None and saved >= limit:
                break

            # ---- Wall-clock budget
            elapsed = time.time() - start_time
            if elapsed > self._WALL_CLOCK_LIMIT:
                print(
                    f"[{self.site_id}] wall-clock budget ({self._WALL_CLOCK_LIMIT}s) reached"
                    f" after {elapsed:.0f}s — stopping cleanly"
                )
                break

            # ---- Progress log every 10 pages
            if page_num > 0 and page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            # ---- Safety-cap log
            if page_num == self._MAX_PAGES - 1:
                print(
                    f"[{self.site_id}] reached safety cap of {self._MAX_PAGES} pages"
                )

            # ---- Fetch
            html = self._fetch_page_html(page_num)
            if not html:
                print(
                    f"[{self.site_id}] page {page_num}: fetch failed after 3 retries — stopping"
                )
                break

            # ---- Parse
            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] page {page_num}: no items found — end of pagination")
                break

            # ---- Loop-detection: all items already seen → paginator looped back
            new_count = sum(1 for it in items if it["pdf_url"] not in seen_urls)
            if new_count == 0:
                print(
                    f"[{self.site_id}] page {page_num}: all {len(items)} items already seen"
                    f" — pagination loop detected, stopping"
                )
                break

            # ---- Process items
            for item in items:
                if limit is not None and saved >= limit:
                    break

                try:
                    pdf_url = item["pdf_url"]

                    # URL dedup
                    if pdf_url in seen_urls:
                        continue
                    seen_urls.add(pdf_url)

                    title = item["title"]
                    abstract = item["abstract"]
                    date_str = item["date"]

                    if not title:
                        print(f"[{self.site_id}] skipping: empty title (url={pdf_url[:60]})")
                        continue

                    # Abstract length guard (requirement: skip <50; test requires >=100)
                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] skipping short abstract"
                            f" ({len(abstract)} chars): {title[:50]}"
                        )
                        continue

                    # ---- Build external_id / original_filename from S3 path
                    try:
                        parsed_url = urllib.parse.urlparse(pdf_url)
                        raw_filename = parsed_url.path.split("/")[-1]
                        filename = urllib.parse.unquote(raw_filename)
                    except Exception:
                        filename = pdf_url.split("/")[-1]

                    # external_id: use the decoded S3 filename as stable unique key
                    external_id = filename

                    paper = {
                        "site_id": self.site_id,
                        "external_id": external_id,
                        # url → meta_url via libertree_adapter
                        "url": pdf_url,
                        "pdf_url": pdf_url,
                        "title": title,
                        "abstract": abstract,
                        "published_date": date_str,
                        "posted_date": date_str,
                        "publisher": "Arts et Métiers",
                        "original_filename": filename,
                        "metadata": json.dumps(
                            {
                                "posted_date": date_str,
                                "originalFilename": filename,
                                "s3_url": pdf_url,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item failed: {exc}")
                    continue

            # Rate-limit between page fetches
            time.sleep(self._delay)

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved
