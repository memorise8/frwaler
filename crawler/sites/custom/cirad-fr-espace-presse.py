# -*- coding: utf-8 -*-
"""Crawler for CIRAD Espace Presse - Communiqués de presse.

List page:   https://www.cirad.fr/espace-presse/communiques-de-presse?page=N
Detail page: https://www.cirad.fr/espace-presse/communiques-de-presse/{year}/{slug}
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from html import unescape
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

# ---------------------------------------------------------------------------
# French month name → zero-padded month number
# ---------------------------------------------------------------------------
_FR_MONTHS = {
    "janvier": "01", "février": "02", "fevrier": "02",
    "mars": "03", "avril": "04", "mai": "05", "juin": "06",
    "juillet": "07", "août": "08", "aout": "08",
    "septembre": "09", "octobre": "10", "novembre": "11", "décembre": "12",
    "decembre": "12",
}


def _parse_french_date(text: str) -> str | None:
    """'29 avril 2026' → '2026-04-29'. Returns None if unparseable."""
    if not text:
        return None
    t = text.strip().lower()
    m = re.match(r"(\d{1,2})\s+(\S+)\s+(\d{4})", t)
    if not m:
        return None
    day, month_fr, year = m.groups()
    month = _FR_MONTHS.get(month_fr)
    if not month:
        return None
    return f"{year}-{month}-{int(day):02d}"


def _strip_html(html: str) -> str:
    """Remove tags and decode entities; collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", html)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _make_soup(raw: str):
    """Return a BeautifulSoup object; tries html5lib → lxml → html.parser."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(raw, parser)
        except Exception:
            continue
    return None


class CiradFrEspacePresseCrawler(BaseCrawler):
    site_id = "cirad-fr-espace-presse"
    site_name = "Custom: cirad-fr-espace-presse"
    base_url = "https://www.cirad.fr"

    LIST_URL = "https://www.cirad.fr/espace-presse/communiques-de-presse"
    MAX_PAGES = 200          # safety cap
    DETAIL_DELAY = 1.0       # seconds between detail fetches
    BACKOFF = (1, 3, 9)      # retry delays in seconds
    MIN_ABSTRACT = 50        # skip items with shorter abstracts
    CURL_TIMEOUT = 30

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        saved = 0
        seen_urls: set[str] = set()
        limit_str = str(limit) if limit is not None else "inf"
        wall_start = time.time()
        MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # 25 minutes

        for page_num in range(1, self.MAX_PAGES + 1):
            # ── wall-clock guard ────────────────────────────────────────
            if time.time() - wall_start > MAX_WALL:
                print(f"[{self.site_id}] wall-clock budget reached at page {page_num}; stopping")
                break

            # ── limit guard ─────────────────────────────────────────────
            if limit is not None and saved >= limit:
                break

            # ── fetch list page ─────────────────────────────────────────
            url = self.LIST_URL if page_num == 1 else f"{self.LIST_URL}?page={page_num}"
            raw = self._fetch(url, context=f"list p{page_num}")
            if not raw:
                print(f"[{self.site_id}] list page {page_num} failed; stopping")
                break

            try:
                soup = _make_soup(raw)
            except Exception as exc:
                print(f"[{self.site_id}] list page {page_num} parse error: {exc}; stopping")
                break
            if soup is None:
                print(f"[{self.site_id}] list page {page_num} unparseable; stopping")
                break

            cards = soup.find_all("article", class_=re.compile(r"\bCard\b"))
            if not cards:
                print(f"[{self.site_id}] page {page_num}: no cards found; stopping")
                break

            new_on_page = 0
            for card in cards:
                if limit is not None and saved >= limit:
                    break

                # ── extract list-page fields ─────────────────────────
                link_el = card.find("a", class_="Card-link")
                if not link_el or not link_el.get("href"):
                    continue
                rel = link_el["href"]
                full_url = urljoin(self.base_url, rel)

                if full_url in seen_urls:
                    continue
                seen_urls.add(full_url)
                new_on_page += 1

                title_el = card.find("h3", class_="Card-title")
                list_title = title_el.get_text(strip=True) if title_el else ""

                date_el = card.find("span", class_=re.compile(r"\bCard-date\b"))
                list_date_raw = date_el.get_text(strip=True) if date_el else ""
                list_date = _parse_french_date(list_date_raw)

                cat_el = card.find("span", class_=re.compile(r"\bCard-thematic\b"))
                list_category = cat_el.get_text(strip=True) if cat_el else ""

                tags = [t.get_text(strip=True) for t in card.find_all("a", class_=re.compile(r"\bTags\b"))]

                # slug = last path segment → use as external_id / post_number
                slug = rel.rstrip("/").split("/")[-1]

                # ── fetch & parse detail page ────────────────────────
                try:
                    item_saved = self._process_detail(
                        full_url=full_url,
                        slug=slug,
                        list_title=list_title,
                        list_date_raw=list_date_raw,
                        list_date=list_date,
                        list_category=list_category,
                        tags=tags,
                    )
                    if item_saved:
                        saved += 1
                    time.sleep(self.DETAIL_DELAY)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {full_url} failed: {exc}")
                    continue

            # ── progress logging ─────────────────────────────────────────
            if page_num % 10 == 0:
                print(f"[{self.site_id}] page {page_num}: saved {saved}/{limit_str}")

            # ── end-of-pagination detection ──────────────────────────────
            if new_on_page == 0:
                print(f"[{self.site_id}] page {page_num}: no new items; stopping")
                break

            # Check for rel="next" link; absent → last page
            has_next = bool(soup.find("a", rel="next"))
            if not has_next:
                print(f"[{self.site_id}] page {page_num}: last page reached")
                break

        else:
            print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached")

        return saved

    # ------------------------------------------------------------------
    # Detail page
    # ------------------------------------------------------------------

    def _process_detail(
        self,
        full_url: str,
        slug: str,
        list_title: str,
        list_date_raw: str,
        list_date: str | None,
        list_category: str,
        tags: list[str],
    ) -> bool:
        """Fetch + parse one detail page; save via _save_paper. Returns True if saved."""
        raw = self._fetch(full_url, context=f"detail {slug}")
        if not raw:
            print(f"[{self.site_id}] detail fetch failed: {full_url}")
            return False

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error {full_url}: {exc}")
            return False
        if soup is None:
            print(f"[{self.site_id}] detail unparseable: {full_url}")
            return False

        # Title
        h1 = soup.find("h1", class_="Hero-title")
        title = h1.get_text(strip=True) if h1 else list_title
        if not title:
            title = list_title or slug

        # Abstract — prefer Hero-desc (the chapo/lead paragraph)
        desc_el = soup.find("div", class_="Hero-desc")
        abstract = desc_el.get_text(strip=True) if desc_el else ""

        # Extend abstract from richtext body paragraphs if it's too short
        if len(abstract) < 100:
            for rt in soup.find_all("div", class_="ezrichtext-field"):
                for p in rt.find_all("p"):
                    txt = p.get_text(strip=True)
                    if len(txt) < 30:
                        continue
                    abstract = (abstract + " " + txt).strip() if abstract else txt
                    if len(abstract) >= 100:
                        break
                if len(abstract) >= 100:
                    break

        if len(abstract) < self.MIN_ABSTRACT:
            print(f"[{self.site_id}] skipping {full_url}: abstract too short ({len(abstract)} chars)")
            return False

        # Published date — prefer OG meta (ISO), fall back to Hero-date, then list date
        published_date = None
        pub_meta = soup.find("meta", property="article:published_time")
        if pub_meta and pub_meta.get("content"):
            published_date = pub_meta["content"][:10]  # YYYY-MM-DD
        if not published_date:
            hero_date_el = soup.find("span", class_="Hero-date")
            if hero_date_el:
                published_date = _parse_french_date(hero_date_el.get_text(strip=True))
        if not published_date:
            published_date = list_date

        # Category
        hero_cat_el = soup.find("span", class_="Hero-cat")
        category = hero_cat_el.get_text(strip=True) if hero_cat_el else list_category

        # Keywords from card tags
        keywords = ", ".join(tags) if tags else None

        # Metadata — store raw fields not mapped to top-level columns
        meta: dict = {
            "posted_date": list_date_raw,
            "category": category,
            "slug": slug,
        }

        paper = {
            "site_id": self.site_id,
            "external_id": slug,
            "post_number": slug,
            "url": full_url,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": list_date,
            "listed_date": list_date,
            "publisher": "CIRAD",
            "keywords": keywords,
            "category": category,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # HTTP helper
    # ------------------------------------------------------------------

    def _fetch(self, url: str, context: str = "") -> str | None:
        """Fetch URL via curl with retries and exponential backoff."""
        for attempt, backoff in enumerate(self.BACKOFF):
            try:
                result = subprocess.run(
                    [
                        "curl", "--tls-max", "1.3", "-sk", "-L",
                        "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.8",
                        "-H", (
                            "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "--max-time", str(self.CURL_TIMEOUT),
                        url,
                    ],
                    capture_output=True,
                    timeout=self.CURL_TIMEOUT + 10,
                )
                body = result.stdout.decode("utf-8", errors="replace")
                if body:
                    return body
                err = f"empty response (exit {result.returncode})"
            except subprocess.TimeoutExpired:
                err = "subprocess timeout"
            except Exception as exc:
                err = str(exc)

            if attempt < len(self.BACKOFF) - 1:
                print(f"[{self.site_id}] {context}: fetch error ({err}); retry in {backoff}s")
                time.sleep(backoff)

        print(f"[{self.site_id}] {context}: all retries failed for {url}")
        return None
