# -*- coding: utf-8 -*-
"""Crawler for Réseau Canopé — Espace Presse (press releases and dossiers).

Source: https://www.reseau-canope.fr/espace-presse/nos-dossiers-et-communiques-de-presse.html

The page is a single TYPO3 CMS page listing all press releases in
`<ul class="ce-uploads">` blocks, organised by year section headers.
Each `<li>` contains a PDF link, a display title (with month/year appended
after `|`), a description span, and a file-size span.  There is no separate
detail page and no API — everything needed is on the list page itself.

The CAS gateway redirect (gateway=true) is transparent: following redirects
(-L in curl) lands on the page without a login wall.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from html import unescape

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# French month → zero-padded number
# ---------------------------------------------------------------------------
_MONTH_FR = {
    "janvier": "01", "février": "02", "mars": "03", "avril": "04",
    "mai": "05", "juin": "06", "juillet": "07", "août": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
}

_BACKOFF = (1, 3, 9)
_MIN_ABSTRACT = 100


def _parse_date_fr(text: str) -> str | None:
    """'Mai 2026' → '2026-05', 'Décembre 2025' → '2025-12', '2025' → '2025'."""
    if not text:
        return None
    low = text.strip().lower()
    year_m = re.search(r"\d{4}", low)
    year = year_m.group() if year_m else None
    for name, num in _MONTH_FR.items():
        if name in low and year:
            return f"{year}-{num}"
    return year


def _curl_fetch(url: str, cookies_file: str | None = None, retries: int = 3) -> str | None:
    """Fetch *url* with curl, following redirects, ignoring TLS errors."""
    cmd = [
        "curl", "--tls-max", "1.3", "-sk", "-L",
        "-A",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
    ]
    if cookies_file:
        cmd += ["-c", cookies_file, "-b", cookies_file]
    cmd.append(url)

    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=45)
            if result.returncode == 0 and result.stdout:
                return result.stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            pass
        except Exception as exc:
            print(f"[reseau-canope-fr-espace-presse] curl error attempt {attempt+1}: {exc}")
        if attempt < retries - 1:
            wait = _BACKOFF[attempt]
            print(f"[reseau-canope-fr-espace-presse] retrying in {wait}s (attempt {attempt+2}/{retries})")
            time.sleep(wait)
    return None


def _make_soup(html: str, context: str = ""):
    """Parse HTML with html5lib → lxml → html.parser fallback."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception as exc:
            print(f"[reseau-canope-fr-espace-presse] BeautifulSoup({parser}) {context}: {exc}")
    return None


def _clean_desc(raw: str) -> str:
    """Strip HTML entities, collapse whitespace, remove trailing 'NNN Ko' artifact."""
    text = unescape(raw)
    text = re.sub(r"\s+", " ", text).strip()
    # The description span sometimes ends with the file size (e.g. "296 Ko")
    text = re.sub(r"\s*\d[\d\s]*Ko\.?\s*$", "", text, flags=re.IGNORECASE).strip()
    return text


# ---------------------------------------------------------------------------
# Crawler class
# ---------------------------------------------------------------------------

class ReseauCanopeFrEspacePressCrawler(BaseCrawler):
    site_id = "reseau-canope-fr-espace-presse"
    site_name = "Custom: reseau-canope-fr-espace-presse"
    base_url = "https://www.reseau-canope.fr"

    LIST_URL = (
        "https://www.reseau-canope.fr"
        "/espace-presse/nos-dossiers-et-communiques-de-presse.html"
    )
    COOKIES_FILE = "/tmp/reseau_canope_crawl_cookies.txt"

    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        limit_or_inf = limit if limit is not None else float("inf")
        wall_start = time.time()
        MAX_WALL_SECONDS = 25 * 60

        page = 1
        page_url = self.LIST_URL

        while True:
            # Wall-clock budget
            if time.time() - wall_start > MAX_WALL_SECONDS:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached, stopping")
                break

            # Safety cap
            if page > 200:
                print(f"[{self.site_id}] Safety cap of 200 pages reached, stopping")
                break

            # Progress every 10 pages
            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_or_inf}")

            html = _curl_fetch(page_url, cookies_file=self.COOKIES_FILE)
            if not html:
                print(f"[{self.site_id}] failed to fetch page {page} ({page_url}), stopping")
                break

            items = self._parse_list_page(html)
            if not items:
                print(f"[{self.site_id}] page {page}: no items found, stopping")
                break

            new_on_page = 0
            for item in items:
                if saved >= limit_or_inf:
                    break

                pdf_url = item.get("pdf_url", "")
                if pdf_url in seen_urls:
                    continue
                seen_urls.add(pdf_url)
                new_on_page += 1

                try:
                    self._save_item(item)
                    saved += 1
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item '{item.get('title', '')[:60]}' failed: {exc}")
                    continue

                time.sleep(self._delay)

            if saved >= limit_or_inf:
                break

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new items (all seen), stopping")
                break

            next_url = self._find_next_page(html, page)
            if not next_url:
                # Single-page site — done
                break

            page_url = next_url
            page += 1

        print(f"[{self.site_id}] crawl complete: saved {saved} items")
        return saved

    # ------------------------------------------------------------------
    # Parsing
    # ------------------------------------------------------------------

    def _parse_list_page(self, html: str) -> list[dict]:
        """Return a list of raw item dicts from the TYPO3 uploads list."""
        soup = _make_soup(html, context="list page")
        if soup is None:
            return []

        items: list[dict] = []
        current_year: int | None = None

        # Walk every element; year headings set context for following <li>s
        for el in soup.find_all(["h2", "li"]):
            if el.name == "h2":
                mark = el.find("mark")
                if mark:
                    try:
                        current_year = int(mark.get_text(strip=True))
                    except (ValueError, TypeError):
                        pass
                continue

            # --- <li> items ---
            link = el.find("a", href=re.compile(r"\.pdf", re.IGNORECASE))
            if link is None:
                continue

            href = link.get("href", "").strip()
            if not href:
                continue

            pdf_url = (
                self.base_url + href if href.startswith("/") else href
            )

            # Title: prefer ce-uploads-fileName span, then link title attr
            fn_span = el.find("span", class_="ce-uploads-fileName")
            raw_title = (
                fn_span.get_text(strip=True) if fn_span
                else unescape(link.get("title", "") or link.get_text(strip=True))
            )

            # Split "Title text | Month Year"
            title = raw_title
            date_str: str | None = None
            if "|" in raw_title:
                head, tail = raw_title.rsplit("|", 1)
                title = head.strip()
                date_str = _parse_date_fr(tail.strip())

            # Fallback date: use year section header
            if not date_str and current_year:
                date_str = str(current_year)

            # Abstract from description span
            desc_span = el.find("span", class_="ce-uploads-description")
            abstract = _clean_desc(desc_span.get_text()) if desc_span else ""

            if len(abstract) < _MIN_ABSTRACT:
                print(
                    f"[{self.site_id}] skip (abstract {len(abstract)} chars < {_MIN_ABSTRACT}): "
                    f"{title[:70]}"
                )
                continue

            # Original filename from URL path
            original_filename = href.rstrip("/").split("/")[-1]

            # Use filename as external_id (stable, unique per PDF)
            external_id = original_filename

            # Try to extract a numeric post_number from filename
            num_m = re.search(r"(\d{5,})", original_filename)
            post_number = num_m.group(1) if num_m else None

            items.append({
                "external_id": external_id,
                "post_number": post_number,
                "title": title,
                "abstract": abstract,
                "url": pdf_url,          # no separate HTML detail page
                "pdf_url": pdf_url,
                "published_date": date_str,
                "listed_date": date_str,
                "publisher": "Réseau Canopé",
                "category": "Communiqué de presse",
                "original_filename": original_filename,
                "year": current_year,
            })

        return items

    def _find_next_page(self, html: str, current_page: int) -> str | None:
        """Return next-page URL if pagination exists; None otherwise."""
        soup = _make_soup(html, context="pagination")
        if soup is None:
            return None
        for a in soup.find_all("a", href=True):
            text = a.get_text(strip=True).lower()
            if "suivant" in text or "page suivante" in text:
                href = a["href"]
                if href.startswith("/"):
                    return self.base_url + href
                if href.startswith("http"):
                    return href
        return None

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_item(self, item: dict) -> None:
        paper = {
            "site_id": self.site_id,
            "external_id": item["external_id"],
            "post_number": item.get("post_number"),
            "title": item["title"],
            "abstract": item["abstract"],
            "url": item["url"],
            "pdf_url": item["pdf_url"],
            "published_date": item.get("published_date"),
            "listed_date": item.get("listed_date"),
            "publisher": item.get("publisher"),
            "category": item.get("category"),
            "original_filename": item.get("original_filename"),
            "metadata": json.dumps(
                {
                    "year": item.get("year"),
                    "originalFilename": item.get("original_filename"),
                },
                ensure_ascii=False,
            ),
        }
        self._save_paper(paper)
