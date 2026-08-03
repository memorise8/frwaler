# -*- coding: utf-8 -*-
"""Crawler for Ministère de la Santé (sante.gouv.fr) — Documentation et publications officielles.

Access strategy
---------------
The site uses F5 TSPD bot-mitigation that requires live JavaScript execution for each page.
TSPD cookies have a 30-second TTL, making the old harvest-and-curl strategy unreliable.

This crawler keeps a single Playwright browser session open for the entire crawl.
Playwright resolves each TSPD challenge automatically via `wait_for_selector` (waiting for
a real-page CSS selector to appear, which only exists after the challenge clears).

Crawl flow
----------
1. Open Playwright browser (Chromium, headless, with AutomationControlled disabled).
2. Load the main documentation index to discover subcategory URLs from the sommaire.
3. For each subcategory, walk the listing using SPIP's ?max_articles=N pagination.
4. For each article URL, navigate and wait for real content, then extract:
   title, date, abstract (.texte--editorial), PDF link, tags, SPIP numeric ID.
5. Persist via _save_paper().
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Optional

_pkg_root = Path(__file__).resolve().parents[3]
if str(_pkg_root) not in sys.path:
    sys.path.insert(0, str(_pkg_root))

from crawler.base_crawler import BaseCrawler  # noqa: E402


# ---------------------------------------------------------------------------
# HTML / parsing helpers (module-level, no class dependency)
# ---------------------------------------------------------------------------

def _bs4_parse(html: str):
    """Parse HTML with html5lib → lxml → html.parser fallback chain."""
    for parser in ("html5lib", "lxml", "html.parser"):
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(html, parser)
        except Exception:
            continue
    return None


def _parse_date(text: str) -> Optional[str]:
    """Convert French date text to YYYY-MM-DD.

    Handles:
    - ``publié le13.02.19``  (DD.MM.YY)
    - ``13.02.2019``         (DD.MM.YYYY)
    - ``13 février 2019``    (FR long)
    - ISO datetime attr ``2026-02-25T16:51:40Z``
    """
    if not text:
        return None
    # ISO date prefix (from datetime= attributes)
    m = re.match(r'(\d{4}-\d{2}-\d{2})', text.strip())
    if m:
        return m.group(1)

    text = re.sub(r'publi[eé]\s+le\s*', '', text, flags=re.I).strip()

    m = re.search(r'(\d{1,2})[./](\d{1,2})[./](\d{2,4})', text)
    if m:
        day, month, year = m.groups()
        if len(year) == 2:
            y = int(year)
            year = str(2000 + y if y <= 30 else 1900 + y)
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    _FR_MONTHS = {
        'janvier': '01', 'février': '02', 'mars': '03', 'avril': '04',
        'mai': '05', 'juin': '06', 'juillet': '07', 'août': '08',
        'septembre': '09', 'octobre': '10', 'novembre': '11', 'décembre': '12',
    }
    month_pat = '|'.join(_FR_MONTHS)
    m = re.search(r'(\d{1,2})\s+(' + month_pat + r')\s+(\d{4})', text, re.I)
    if m:
        day, mon, year = m.groups()
        return f"{year}-{_FR_MONTHS[mon.lower()]}-{day.zfill(2)}"

    return None


def _spip_id_from_html(html: str) -> Optional[str]:
    """Extract SPIP article numeric ID from CSS class ``article-titre-NNNNN``."""
    m = re.search(r'article-titre-(\d+)', html)
    return m.group(1) if m else None


def _normalize_url(href: str, base: str = "https://sante.gouv.fr") -> str:
    href = href.strip()
    if href.startswith("http://") or href.startswith("https://"):
        return href
    return base.rstrip("/") + "/" + href.lstrip("/")


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

class SanteGouvFrMinistereCrawler(BaseCrawler):

    site_id   = "sante-gouv-fr-ministere"
    site_name = "Custom: sante-gouv-fr-ministere"
    base_url  = "https://sante.gouv.fr"

    _START_URL      = "https://sante.gouv.fr/ministere/documentation-et-publications-officielles/"
    _CRAWL_BUDGET_S = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))   # 25-minute wall-clock limit
    _RATE_SLEEP     = 1.0       # seconds between detail fetches

    # CSS selector present on real pages but absent from TSPD challenge pages.
    # wait_for_selector() blocks until it appears (i.e., challenge cleared).
    _REAL_SELECTOR  = ".main-article, .article-rubrique, .noisette__container, .page__titre"

    # ------------------------------------------------------------------
    # Playwright helpers
    # ------------------------------------------------------------------

    def _make_page(self):
        """Start a Playwright Chromium session and return the Page object."""
        from playwright.sync_api import sync_playwright
        self._pw      = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = self._browser.new_context(
            user_agent  = self.USER_AGENT,
            locale      = "fr-FR",
            timezone_id = "Europe/Paris",
            viewport    = {"width": 1280, "height": 900},
        )
        return ctx.new_page()

    def _close_browser(self):
        try:
            self._browser.close()
        except Exception:
            pass
        try:
            self._pw.stop()
        except Exception:
            pass

    def _fetch(self, page, url: str, max_wait_s: int = 60, retries: int = 3) -> Optional[str]:
        """Navigate to *url* and wait until the TSPD challenge clears.

        Returns the rendered HTML string, or None after all retries fail.
        """
        backoffs = [1, 3, 9]
        for attempt in range(retries):
            if attempt > 0:
                wait = backoffs[min(attempt - 1, len(backoffs) - 1)]
                print(f"[{self.site_id}] retry {attempt}/{retries} for {url} in {wait}s")
                time.sleep(wait)
            try:
                page.goto(url, wait_until="commit", timeout=15_000)
            except Exception:
                pass
            try:
                page.wait_for_selector(self._REAL_SELECTOR, timeout=max_wait_s * 1000)
            except Exception:
                pass
            time.sleep(1)
            # page.content() may raise if page is still navigating; retry a few times
            for _ in range(5):
                try:
                    return page.content()
                except Exception:
                    time.sleep(3)
        return None

    # ------------------------------------------------------------------
    # Category discovery
    # ------------------------------------------------------------------

    def _get_category_urls(self, index_html: str) -> list[str]:
        """Return subcategory URLs from the sommaire nav on the index page."""
        try:
            soup = _bs4_parse(index_html)
        except Exception:
            soup = None
        if not soup:
            return [self._START_URL]

        nav = soup.find(class_="navigation-sommaire")
        urls: list[str] = []
        seen: set[str] = set()
        if nav:
            for a in nav.find_all("a", href=True):
                href = a["href"]
                if not href or href.startswith("#"):
                    continue
                if "documentation-et-publications" not in href:
                    continue
                full = _normalize_url(href)
                if full not in seen:
                    seen.add(full)
                    urls.append(full)
        return urls if urls else [self._START_URL]

    # ------------------------------------------------------------------
    # Listing pagination: SPIP max_articles scheme
    # ------------------------------------------------------------------

    def _collect_article_urls(
        self, pw_page, cat_url: str, max_count: int = 9999
    ) -> list[str]:
        """Walk a listing page using ?max_articles=N links (cumulative load-more).

        Returns deduplicated absolute article URLs found up to *max_count*.
        """
        seen:   set[str]  = set()
        result: list[str] = []
        max_articles_param: Optional[int] = None  # None = default first page

        for page_n in range(200):  # Safety cap: 200 pagination steps
            if len(result) >= max_count:
                break

            url = (
                cat_url
                if max_articles_param is None
                else f"{cat_url}?max_articles={max_articles_param}"
            )
            html = self._fetch(pw_page, url)
            if not html:
                print(f"[{self.site_id}] listing fetch failed: {url}")
                break

            try:
                soup = _bs4_parse(html)
            except Exception:
                soup = None
            main_el = soup.find("main") if soup else None
            if not main_el:
                print(f"[{self.site_id}] no <main> on listing: {url}")
                break

            new_found: list[str] = []
            for a in main_el.find_all("a", href=True):
                href = a["href"]
                if "/article/" not in href:
                    continue
                full = _normalize_url(href)
                if full not in seen:
                    seen.add(full)
                    new_found.append(full)

            result.extend(new_found)

            cat_label = cat_url.rstrip("/").split("/")[-1] or "root"
            if page_n % 10 == 0:
                print(
                    f"[{self.site_id}] listing '{cat_label}' p{page_n+1}: "
                    f"{len(new_found)} new, {len(result)} total"
                )

            # Find the next max_articles pagination link
            more_param: Optional[int] = None
            for a in main_el.find_all("a", href=True):
                m = re.search(r"max_articles=(\d+)", a.get("href", ""))
                if m:
                    more_param = int(m.group(1))
                    break

            if more_param is None or not new_found:
                break
            max_articles_param = more_param

        return result

    # ------------------------------------------------------------------
    # Article detail parsing
    # ------------------------------------------------------------------

    def _parse_detail(self, html: str, url: str) -> Optional[dict]:
        """Parse a sante.gouv.fr article page and return a paper dict or None.

        Returns None (and logs) if the abstract is < 50 chars.
        """
        try:
            soup = _bs4_parse(html)
        except Exception:
            soup = None
        if not soup:
            return None

        main_el = soup.find("main") or soup

        # --- Title ---
        titre_el = main_el.find(class_=re.compile(r"main-article__titre"))
        if not titre_el:
            titre_el = main_el.find("h1") or main_el.find("h2") or main_el.find("h3")
        title = titre_el.get_text(strip=True) if titre_el else ""
        if not title:
            title = url.rstrip("/").rsplit("/", 1)[-1].replace("-", " ").title()

        # --- SPIP numeric article ID (from CSS class article-titre-NNNNN) ---
        spip_id = _spip_id_from_html(html)

        # --- Published date ---
        # Prefer <time datetime="YYYY-MM-DD..."> attribute; fall back to text
        published_date: Optional[str] = None
        time_el = main_el.find("time", class_=re.compile(r"date"))
        if time_el:
            dt_attr = time_el.get("datetime", "")
            published_date = _parse_date(dt_attr) if dt_attr else _parse_date(time_el.get_text())
        if not published_date:
            date_el = main_el.find(class_=re.compile(r"main-article__date|date--publication"))
            if date_el:
                published_date = _parse_date(date_el.get_text())

        # --- Abstract ---
        # Primary: .texte--editorial (the full editorial text block)
        abstract_el = (
            main_el.find(class_="texte--editorial")
            or main_el.find(class_=re.compile(r"main-article__texte|article-texte"))
        )
        if abstract_el:
            # Remove social-share / tag widgets that might be nested
            for el in abstract_el.find_all(
                class_=re.compile(r"tag|share|partage|nav|picto")
            ):
                el.decompose()
            abstract = abstract_el.get_text(separator="\n", strip=True)
        else:
            abstract = ""

        # Fallback: subtitle (chapo) + paragraphs
        if len(abstract) < 50:
            sous_el = main_el.find(class_=re.compile(r"main-article__sous-titre"))
            chapo   = sous_el.get_text(separator=" ", strip=True) if sous_el else ""
            paras   = [
                p.get_text(separator=" ", strip=True)
                for p in main_el.find_all("p")
                if len(p.get_text(strip=True)) > 30
            ]
            abstract = (chapo + "\n\n" + " ".join(paras)).strip() if chapo else " ".join(paras)
            abstract = re.sub(r"\s{2,}", " ", abstract).strip()[:1000]

        if len(abstract) < 50:
            print(
                f"[{self.site_id}] abstract too short ({len(abstract)} chars) "
                f"— skipping {url}"
            )
            return None

        # --- PDF / attachment URL ---
        pdf_url: Optional[str] = None
        original_filename: Optional[str] = None
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if ".pdf" in href.lower() or ("/IMG/" in href and not href.endswith("/")):
                pdf_url = _normalize_url(href)
                original_filename = href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
                break

        # --- Keywords / tags ---
        tag_texts: list[str] = []
        for t in main_el.find_all(class_=re.compile(r"\bmain-article__tag\b")):
            # Skip the container (.main-article__tags plural)
            classes = t.get("class") or []
            if "main-article__tags" not in classes:
                txt = t.get_text(strip=True)
                if txt:
                    tag_texts.append(txt)
        keywords = ", ".join(tag_texts) if tag_texts else None

        # --- Category from breadcrumb / URL path ---
        bc_el = soup.find(class_=re.compile(r"fil-ariane|ariane|breadcrumb"))
        if bc_el:
            crumbs = [a.get_text(strip=True) for a in bc_el.find_all("a") if a.get_text(strip=True)]
            category = " > ".join(crumbs[1:]) if len(crumbs) > 1 else (crumbs[0] if crumbs else "")
        else:
            # Derive from URL: …/rapports/[subcat]/article/slug
            parts = url.split("/")
            try:
                art_idx = parts.index("article")
                category = " > ".join(p for p in parts[3:art_idx] if p)
            except ValueError:
                category = ""

        # --- Subtitle ---
        sub_el  = main_el.find(class_=re.compile(r"main-article__sous-titre"))
        subtitle = sub_el.get_text(strip=True) if sub_el else ""

        # --- IDs ---
        url_slug    = url.rstrip("/").rsplit("/", 1)[-1]
        external_id = spip_id or url_slug
        post_number = spip_id  # numeric SPIP article ID string

        # --- Metadata dict ---
        meta: dict = {
            "slug":          url_slug,
            "posted_date":   published_date,   # libertree_adapter uses this key
            "originalFilename": original_filename,
        }
        if spip_id:
            meta["spip_id"] = spip_id
        if subtitle:
            meta["subtitle"] = subtitle[:500]

        return {
            "site_id":           self.site_id,
            "external_id":       external_id,
            "post_number":       post_number,
            "title":             title,
            "abstract":          abstract,
            "published_date":    published_date,
            "listed_date":       published_date,
            "url":               url,
            "pdf_url":           pdf_url,
            "original_filename": original_filename,
            "keywords":          keywords,
            "category":          category,
            "publisher":         "Ministère de la Santé",
            "metadata":          json.dumps(meta, ensure_ascii=False),
        }

    # ------------------------------------------------------------------
    # Public crawl entry point
    # ------------------------------------------------------------------

    def crawl(self, limit=None) -> int:
        """Crawl documentation publications from sante.gouv.fr.

        Parameters
        ----------
        limit : int or None
            Maximum records to save (None = unlimited).

        Returns
        -------
        int  Number of records actually saved.
        """
        try:
            import playwright  # noqa: F401
        except ImportError:
            print(
                f"[{self.site_id}] ERROR: playwright not installed — "
                "run: pip install playwright && playwright install chromium"
            )
            return 0

        limit_str  = str(limit) if limit is not None else "∞"
        saved      = 0
        seen_urls: set[str] = set()
        start_ts   = time.time()

        pw_page = self._make_page()
        try:
            # ── Step 1: Load index → discover category URLs ─────────────────
            print(f"[{self.site_id}] Loading documentation index …", flush=True)
            index_html = self._fetch(pw_page, self._START_URL)
            if not index_html:
                print(f"[{self.site_id}] Failed to load index — aborting.")
                return saved

            cat_urls = self._get_category_urls(index_html)
            print(f"[{self.site_id}] Found {len(cat_urls)} category URLs", flush=True)

            # ── Step 2: Collect article URLs from each listing page ──────────
            all_article_urls: list[str] = []
            for cat_url in cat_urls:
                if time.time() - start_ts > self._CRAWL_BUDGET_S:
                    print(f"[{self.site_id}] 25-min budget reached during listing phase")
                    break
                want = (
                    None
                    if limit is None
                    else limit - saved - len(all_article_urls)
                )
                if want is not None and want <= 0:
                    break
                urls = self._collect_article_urls(
                    pw_page, cat_url,
                    max_count=(want if want is not None else 9999),
                )
                for u in urls:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        all_article_urls.append(u)

            print(
                f"[{self.site_id}] Collected {len(all_article_urls)} unique article URLs",
                flush=True,
            )

            # ── Step 3: Fetch and parse each article detail page ────────────
            limit_n = limit if limit is not None else float("inf")

            for i, art_url in enumerate(all_article_urls):
                if saved >= limit_n:
                    break
                if time.time() - start_ts > self._CRAWL_BUDGET_S:
                    print(f"[{self.site_id}] 25-min budget reached at article {i}")
                    break

                if i > 0 and i % 10 == 0:
                    print(
                        f"[{self.site_id}] page {i}: saved {saved}/{limit_str}",
                        flush=True,
                    )

                try:
                    html = self._fetch(pw_page, art_url)
                    if not html:
                        print(f"[{self.site_id}] item {i} fetch failed: {art_url}")
                        continue

                    paper = self._parse_detail(html, art_url)
                    if paper is None:
                        continue

                    self._save_paper(paper)
                    saved += 1
                    time.sleep(self._delay)

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {art_url} failed: {exc}")
                    continue

        finally:
            self._close_browser()

        print(f"[{self.site_id}] Done — saved {saved} documents.", flush=True)
        return saved
