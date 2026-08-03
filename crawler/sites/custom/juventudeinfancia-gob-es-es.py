# -*- coding: utf-8 -*-
"""Crawler for juventudeinfancia.gob.es — Notas de prensa (Drupal 10 HTML).

Target: https://www.juventudeinfancia.gob.es/es/comunicacion/notas-prensa
Pagination: ?page=0 … ?page=N (5 items/page, currently ~55 pages).
"""

import json
import os
import re
import subprocess
import time
from urllib.parse import unquote, urlparse

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.juventudeinfancia.gob.es"
_LIST_URL = _BASE_URL + "/es/comunicacion/notas-prensa"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url, retries=3):
    """GET via curl; returns response text (str) or None on failure."""
    cmd = [
        "curl", "--tls-max", "1.3", "-skL", "--max-time", "30",
        "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
        "-H", "Accept-Language: es-ES,es;q=0.9,en;q=0.8",
        "-H", f"User-Agent: {_UA}",
        url,
    ]
    delays = [1, 3, 9]
    for attempt in range(retries):
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=40)
            text = result.stdout.decode("utf-8", errors="replace")
            if text.strip():
                return text
            if attempt < retries - 1:
                wait = delays[attempt] if attempt < len(delays) else 9
                time.sleep(wait)
        except Exception as exc:
            if attempt < retries - 1:
                wait = delays[attempt] if attempt < len(delays) else 9
                print(f"[juventudeinfancia-gob-es-es] curl error for {url}: {exc}, "
                      f"retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[juventudeinfancia-gob-es-es] curl failed after {retries} attempts "
                      f"for {url}: {exc}")
    return None


def _make_soup(html):
    """Try html5lib → lxml → html.parser; return BeautifulSoup or None."""
    from bs4 import BeautifulSoup
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _clean_text(text):
    """Normalise whitespace in a string."""
    return re.sub(r"\s+", " ", text or "").strip()


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class JuventudeinfanciaGobEsEsCrawler(BaseCrawler):
    """Press-release crawler for Ministerio de Juventud e Infancia (Spain)."""

    site_id = "juventudeinfancia-gob-es-es"
    site_name = "Custom: juventudeinfancia-gob-es-es"
    base_url = _BASE_URL

    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))

    def crawl(self, limit=None):
        """Crawl Notas de prensa, saving up to *limit* items (None = unlimited)."""
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds

        for page in range(self._MAX_PAGES):
            # --- guards ---
            if saved >= limit_or_inf:
                break
            elapsed = time.time() - start_time
            if elapsed > MAX_WALL:
                print(f"[{self.site_id}] 25-minute wall-clock budget reached at page {page}. Stopping.")
                break

            if page % 10 == 0:
                lim_str = str(limit) if limit is not None else "∞"
                print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

            list_url = f"{_LIST_URL}?page={page}"
            html = _curl_get(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch listing page {page}. Stopping.")
                break

            items = self._parse_listing(html)
            if not items:
                print(f"[{self.site_id}] No items on page {page}. Pagination complete.")
                break

            new_items = [it for it in items if it["url"] not in seen_urls]
            if not new_items:
                print(f"[{self.site_id}] All items on page {page} already seen. Done.")
                break
            for it in new_items:
                seen_urls.add(it["url"])

            for item in new_items:
                if saved >= limit_or_inf:
                    break
                if time.time() - start_time > MAX_WALL:
                    break

                try:
                    time.sleep(self._delay)
                    paper = self._fetch_detail(item)
                    if paper is None:
                        continue
                    self._save_paper(paper)
                    saved += 1
                    lim_str = str(limit) if limit is not None else "∞"
                    print(f"[{self.site_id}] Saved {saved}/{lim_str}: {item['title'][:60]}")
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {item.get('url', '?')} failed: {exc}")
                    continue

        if page >= self._MAX_PAGES - 1:
            print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Listing page parser
    # ------------------------------------------------------------------

    def _parse_listing(self, html):
        """Return list of dicts: {title, url, listed_date, intro}."""
        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] parse error on listing: {exc}")
            return []
        if not soup:
            return []

        items = []
        for li in soup.find_all("li"):
            title_div = li.find("div", class_="views-field-title")
            if not title_div:
                continue
            a_tag = title_div.find("a")
            if not a_tag:
                continue
            url_path = a_tag.get("href", "")
            if not url_path.startswith("/es/comunicacion/notas-prensa/"):
                continue
            # Exclude the listing page itself
            if url_path.rstrip("/") == "/es/comunicacion/notas-prensa":
                continue

            title = _clean_text(a_tag.get_text())
            full_url = _BASE_URL + url_path

            # Date from <time datetime="...">
            listed_date = ""
            date_div = li.find("div", class_="views-field-created")
            if date_div:
                t = date_div.find("time")
                if t:
                    dt = t.get("datetime", "")
                    listed_date = dt[:10] if len(dt) >= 10 else ""

            # Short intro from listing
            intro = ""
            intro_div = li.find("div", class_="views-field-field-introduccion")
            if intro_div:
                intro = _clean_text(intro_div.get_text())

            items.append({
                "title": title,
                "url": full_url,
                "listed_date": listed_date,
                "intro": intro,
            })

        return items

    # ------------------------------------------------------------------
    # Detail page fetcher / parser
    # ------------------------------------------------------------------

    def _fetch_detail(self, item):
        """Fetch detail page and return a paper dict, or None to skip."""
        url = item["url"]
        html = None
        for attempt in range(3):
            html = _curl_get(url)
            if html:
                break
            if attempt < 2:
                wait = [1, 3, 9][attempt]
                print(f"[{self.site_id}] Retry {attempt+1}/3 for {url}")
                time.sleep(wait)

        if not html:
            print(f"[{self.site_id}] Failed to fetch detail {url} after 3 attempts; skipping.")
            return None

        try:
            soup = _make_soup(html)
        except Exception as exc:
            print(f"[{self.site_id}] BS4 error for {url}: {exc}; skipping.")
            return None
        if not soup:
            return None

        # --- Node ID (Drupal internal integer ID) ---
        node_id = ""
        article = soup.find("article", attrs={"data-history-node-id": True})
        if article:
            node_id = article.get("data-history-node-id", "")
        if not node_id:
            # Fallback: language-switcher hidden inputs embed /XX/node/NNN
            m = re.search(r'/(?:ca|gl|eu|va|en)/node/(\d+)', html)
            if m:
                node_id = m.group(1)

        # --- Slug from URL (used as external_id) ---
        slug = url.rstrip("/").split("/")[-1]

        # --- Abstract: intro from listing + full body from detail page ---
        abstract = self._extract_abstract(soup, article, item.get("intro", ""))

        if not abstract or len(abstract) < 50:
            print(f"[{self.site_id}] Skipping {slug}: abstract too short "
                  f"({len(abstract) if abstract else 0} chars)")
            return None

        # --- Published date: prefer listing date, fallback to article header ---
        published_date = item.get("listed_date", "")
        if not published_date:
            published_date = self._extract_date_from_page(soup, html)

        # --- PDF links ---
        pdf_url = None
        original_filename = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if re.search(r'\.pdf', href, re.I):
                pdf_url = (_BASE_URL + href) if href.startswith("/") else href
                raw_name = unquote(urlparse(pdf_url).path.split("/")[-1])
                if raw_name:
                    original_filename = raw_name
                break

        metadata = {
            "node_id": node_id,
            "slug": slug,
            "posted_date": item.get("listed_date", ""),
        }

        return {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": node_id if node_id else slug,
            "title": item["title"],
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": item.get("listed_date", ""),
            "url": url,
            "pdf_url": pdf_url,
            "original_filename": original_filename,
            "publisher": "Ministerio de Juventud e Infancia",
            "department": None,
            "authors": None,
            "journal": None,
            "keywords": None,
            "category": "Notas de prensa",
            "doi": None,
            "metadata": json.dumps(metadata, ensure_ascii=False),
        }

    def _extract_abstract(self, soup, article, intro_fallback):
        """Build abstract from detail page body text.

        Prefers the article tag body; falls back to listing intro.
        """
        parts = []

        # Primary: all <p> tags inside <article>
        if article:
            for p in article.find_all("p"):
                txt = _clean_text(p.get_text())
                if txt and txt not in parts:
                    parts.append(txt)

        if parts:
            return " ".join(parts)

        # Fallback 1: field-introduccion anywhere on page
        intro_div = soup.find("div", class_=re.compile(r"field-introduccion"))
        if intro_div:
            txt = _clean_text(intro_div.get_text())
            if txt:
                return txt

        # Fallback 2: <main> tag content, stripping nav/header/footer
        main = soup.find("main")
        if main:
            for tag in main.find_all(["nav", "header", "footer", "script", "style"]):
                tag.decompose()
            txt = _clean_text(main.get_text())
            if len(txt) >= 50:
                return txt

        # Last resort: intro from listing page
        return intro_fallback or ""

    def _extract_date_from_page(self, soup, html):
        """Try to extract a date from the detail page when listing didn't have one."""
        # Article header often contains e.g. "Miércoles 13 de Mayo de 2026"
        _ES_MONTHS = {
            "enero": "01", "febrero": "02", "marzo": "03", "abril": "04",
            "mayo": "05", "junio": "06", "julio": "07", "agosto": "08",
            "septiembre": "09", "octubre": "10", "noviembre": "11", "diciembre": "12",
        }
        m = re.search(r"(\d{1,2})\s+de\s+(\w+)\s+de\s+(\d{4})", html, re.I)
        if m:
            day, month_str, year = m.group(1), m.group(2).lower(), m.group(3)
            month = _ES_MONTHS.get(month_str)
            if month:
                return f"{year}-{month}-{int(day):02d}"
        return ""
