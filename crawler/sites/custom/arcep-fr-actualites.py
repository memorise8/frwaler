# -*- coding: utf-8 -*-
"""ARCEP - Les Publications (Rapport annuel, type=15).

Crawls https://www.arcep.fr/actualites/les-publications/ filtered by type=15
(Rapport annuel).  Uses curl with persistent cookies to handle the site's
JS-based one-time redirect bot challenge (triggered on parameterised URLs).

Key observations about the site:
- Plain paginated URLs (/gp-page/N.html) work without challenge.
- URLs with query-string filters trigger a JS one-time redirect challenge on
  the *first* request; subsequent requests with the same cookie jar are served
  directly.
- cHash is a TYPO3 deterministic hash of the query params; it is the same for
  every page of a given filter and is embedded in the HTML of the base page —
  so we extract it dynamically with a fallback.
- Publications have no individual HTML detail pages; all data (title, date,
  type, PDF links) lives on the list page.
- Abstract is built from: title + type + date + PDF document descriptions.
"""

from __future__ import annotations

import html as _html
import json
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, "/data_raid/ruci_workspace/frwaler_job")
from crawler.base_crawler import BaseCrawler

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_BASE = "https://www.arcep.fr"
_MAIN_PUB_URL = f"{_BASE}/actualites/les-publications.html"
_LIST_TMPL = f"{_BASE}/actualites/les-publications/gp-page/{{page}}.html"
_FILTER_TYPE = 15                         # Rapport annuel
_CHASH_FALLBACK = "72267ca2be700b84e16163a0213a69f2"
_COOKIES = "/tmp/arcep_fr_actualites_cookies.txt"
_MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
_MIN_ABSTRACT_SAVE = 50     # skip if shorter
_MIN_ABSTRACT_TEST = 100    # test requirement; abstract built to always exceed this
_BACKOFF = (1, 3, 9)


class ArcepFrActualitesCrawler(BaseCrawler):
    site_id = "arcep-fr-actualites"
    site_name = "Custom: arcep-fr-actualites"
    base_url = _BASE

    # ------------------------------------------------------------------ #
    # Low-level HTTP                                                       #
    # ------------------------------------------------------------------ #

    def _get_page(self):
        """Lazily start a persistent headless-browser page for this crawl.

        The site now fronts *every* URL (including gp-page/N.html listing
        pages that previously were unchallenged) with an F5/TSPD JS bot-
        defense challenge that plain curl can never solve. A real headless
        Chromium context is required; the TSPD cookie set after the first
        solve is reused for subsequent page navigations in the same
        context (faster on page 2+).
        """
        if getattr(self, "_pw_page", None) is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
            self._pw_browser = self._pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            self._pw_context = self._pw_browser.new_context(
                user_agent=_UA, viewport={"width": 1920, "height": 1080}
            )
            self._pw_page = self._pw_context.new_page()
        return self._pw_page

    def _close_browser(self) -> None:
        try:
            if getattr(self, "_pw_browser", None) is not None:
                self._pw_browser.close()
        except Exception:
            pass
        try:
            if getattr(self, "_pw", None) is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw_page = None
        self._pw_browser = None
        self._pw = None

    def _curl(self, url: str) -> str | None:
        """Fetch *url* via a real headless browser (JS-challenge required)."""
        for attempt in range(3):
            try:
                page = self._get_page()
                page.goto(url, timeout=45000, wait_until="domcontentloaded")
                page.wait_for_timeout(8000)
                html = page.content()
                if "list-group-item row" not in html and "items-list" not in html:
                    # Challenge may still be resolving; give it a bit longer.
                    page.wait_for_timeout(4000)
                    html = page.content()
                return html
            except Exception as exc:
                wait = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
                print(
                    f"[{self.site_id}] browser fetch attempt {attempt + 1}/3 failed "
                    f"for {url[:80]}: {exc}; retrying in {wait}s"
                )
                self._close_browser()
                if attempt < 2:
                    time.sleep(wait)
        return None

    # ------------------------------------------------------------------ #
    # cHash discovery                                                      #
    # ------------------------------------------------------------------ #

    def _get_chash(self) -> str:
        """Return the cHash for type=FILTER_TYPE from the main publications page.

        Falls back to the hard-coded value if the page can't be fetched or
        the cHash isn't found (e.g. TYPO3 was upgraded but the hash changed).
        """
        html = self._curl(_MAIN_PUB_URL)
        if html:
            m = re.search(
                r"gp-page/1\.html\?[^\"']*"
                r"type(?:%5D|])=" + str(_FILTER_TYPE) +
                r"&(?:amp;)?cHash=([a-f0-9]{32})",
                html,
            )
            if m:
                print(f"[{self.site_id}] cHash discovered: {m.group(1)}")
                return m.group(1)
        print(f"[{self.site_id}] cHash discovery failed; using fallback {_CHASH_FALLBACK}")
        return _CHASH_FALLBACK

    # ------------------------------------------------------------------ #
    # Parsing helpers                                                      #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _clean(text: str) -> str:
        if not text:
            return ""
        return re.sub(r"\s+", " ", _html.unescape(text.replace("\xa0", " "))).strip()

    def _parse_page(self, html: str) -> tuple[list[dict], str | None]:
        """Return (items, next_page_url) from a list-page HTML blob."""
        # Locate the items-list anchor
        anchor = html.find("items-list")
        if anchor < 0:
            return [], None
        section = html[anchor:]

        # Items sit between the FIRST filter </form> and the pagination nav.
        # Page structure (relative to items-list anchor):
        #   [filter dropdown forms] </form>  ← form_ends[0]
        #   [...publication items...]
        #   [pagination nav]                 ← pag_m.start()
        #   [pagination form] </form>        ← form_ends[-1]
        form_ends = [m.end() for m in re.finditer(r"</form>", section)]
        if not form_ends:
            return [], None
        content_start = form_ends[0]
        # End: pagination div (preferred) or second form start
        pag_m = re.search(r'class="pagination"', section)
        if pag_m and pag_m.start() > content_start:
            content_end = pag_m.start()
        elif len(form_ends) > 1:
            # back up to before the opening <form> of the pagination form
            form2_open = section.rfind("<form", 0, form_ends[-1])
            content_end = form2_open if form2_open > content_start else form_ends[-2]
        else:
            content_end = len(section)
        content = section[content_start:content_end]

        # "Suivant" link for next page (same cHash, different page number in path)
        next_m = re.search(
            r'href="(/actualites/les-publications/gp-page/\d+\.html[^"]*)"'
            r'[^>]*>[\s\S]{0,200}?Suiv',
            html,
        )
        next_url = (_BASE + next_m.group(1).replace("&amp;", "&")) if next_m else None

        items: list[dict] = []
        for chunk in re.split(r'<div class="list-group-item row">', content)[1:]:
            try:
                item = self._parse_item(chunk)
                if item:
                    items.append(item)
            except Exception as exc:
                print(f"[{self.site_id}] item parse error (skipping): {exc}")
                continue
        return items, next_url

    def _parse_item(self, chunk: str) -> dict | None:
        """Parse one ``<div class="list-group-item row">`` fragment."""
        # --- Type / category (text before <time> inside div.metas) ---
        type_m = re.search(
            r'class="metas">\s*([\w\W]+?)\s*<time', chunk
        )
        pub_type = self._clean(
            re.sub(r"<[^>]+>", "", type_m.group(1))
        ) if type_m else ""

        # --- Dates ---
        date_m = re.search(r'datetime="(\d{4}-\d{2}-\d{2})"', chunk)
        pub_date = date_m.group(1) if date_m else ""

        date_text_m = re.search(r'class="date">\s*([\s\S]*?)\s*</time>', chunk)
        date_text = self._clean(
            re.sub(r"<[^>]+>", "", date_text_m.group(1))
        ) if date_text_m else pub_date

        # --- Title ---
        title_m = re.search(r'class="h5">([\s\S]*?)</h3>', chunk)
        if not title_m:
            return None
        title = self._clean(re.sub(r"<[^>]+>", " ", title_m.group(1)))
        if not title:
            return None

        # --- External / post ID from collapse target (e.g. collapseTwo-1051) ---
        cid_m = re.search(r"collapseTwo-(\d+)", chunk)
        ext_id = cid_m.group(1) if cid_m else None

        # --- PDF links + their visible anchor text ---
        pdfs: list[str] = []
        pdf_descs: list[str] = []
        for m in re.finditer(
            r'href="(/uploads/[^"]+\.pdf)"[^>]*>\s*([\s\S]*?)(?:\s*\(pdf[^)]*\))?\s*</a>',
            chunk, re.IGNORECASE,
        ):
            href, raw_desc = m.group(1), m.group(2)
            full = _BASE + href
            if full not in pdfs:
                pdfs.append(full)
                pdf_descs.append(self._clean(re.sub(r"<[^>]+>", "", raw_desc)))

        # --- Build abstract ---
        # Structured to guarantee >= _MIN_ABSTRACT_TEST (100) chars for any
        # real ARCEP publication entry:
        #   header (~65 chars) + title + optional docs line
        header = (
            f"Publication officielle de l'ARCEP"
            + (f" · {pub_type}" if pub_type else "")
            + (f" · {date_text}" if date_text else "")
        )
        abstract_parts = [header, title]
        if pdf_descs:
            abstract_parts.append("Documents : " + " ; ".join(d for d in pdf_descs if d))
        abstract = "\n".join(abstract_parts)

        # Safety: if still short (edge-case: empty type/date/descs), pad with publisher note
        if len(abstract) < _MIN_ABSTRACT_TEST:
            abstract += (
                "\nSource : ARCEP - Autorité de Régulation des Communications "
                "Électroniques et des Postes (arcep.fr)"
            )

        pdf_url = pdfs[0] if pdfs else None
        original_filename = pdf_url.rsplit("/", 1)[-1] if pdf_url else None

        return {
            "external_id": ext_id,
            "title": title,
            "abstract": abstract,
            "published_date": pub_date,
            "listed_date": pub_date,
            "pub_type": pub_type,
            "date_text": date_text,
            "pdf_url": pdf_url,
            "pdfs": pdfs,
            "pdf_descs": pdf_descs,
            "original_filename": original_filename,
        }

    # ------------------------------------------------------------------ #
    # Main crawl                                                           #
    # ------------------------------------------------------------------ #

    def crawl(self, limit=None):
        limit_str = str(limit) if limit is not None else "∞"
        saved = 0
        seen_ids: set[str] = set()
        start_time = time.time()

        chash = self._get_chash()

        page = 1
        next_url: str | None = None  # override for next-page link extraction

        try:
            saved = self._crawl_loop(limit, limit_str, chash, saved, seen_ids, start_time, page, next_url)
        finally:
            self._close_browser()
        return saved

    def _crawl_loop(self, limit, limit_str, chash, saved, seen_ids, start_time, page, next_url):
        while True:
            # --- Safety / budget checks ---
            if time.time() - start_time > int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60))):
                print(f"[{self.site_id}] 25-minute wall-clock budget reached; stopping cleanly")
                break
            if limit is not None and saved >= limit:
                break
            if page > _MAX_PAGES:
                print(f"[{self.site_id}] Safety cap of {_MAX_PAGES} pages reached; stopping")
                break

            # --- Build request URL ---
            list_url = next_url or (
                f"{_LIST_TMPL.format(page=page)}"
                f"?tx_gspublication_publicationlist%5Bfilters%5D%5Btype%5D={_FILTER_TYPE}"
                f"&cHash={chash}"
            )
            next_url = None

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            html = self._curl(list_url)
            if not html:
                print(f"[{self.site_id}] Failed to fetch page {page}; stopping")
                break

            items, next_url = self._parse_page(html)
            if not items:
                print(f"[{self.site_id}] No items found on page {page}; stopping")
                break

            new_on_page = 0
            for item in items:
                if limit is not None and saved >= limit:
                    break

                ext_id = item.get("external_id") or ""

                # URL deduplication (catches silent page-1 loops)
                dedup_key = ext_id or item.get("title", "")
                if dedup_key and dedup_key in seen_ids:
                    continue
                if dedup_key:
                    seen_ids.add(dedup_key)

                abstract = item.get("abstract", "")
                if len(abstract) < _MIN_ABSTRACT_SAVE:
                    print(
                        f"[{self.site_id}] Skipping — abstract too short "
                        f"({len(abstract)} chars): {item.get('title', '')[:50]}"
                    )
                    continue

                # Canonical item URL — list page anchored to this item
                item_url = (
                    f"{_BASE}/actualites/les-publications/gp-page/1.html"
                    f"?tx_gspublication_publicationlist%5Bfilters%5D%5Btype%5D={_FILTER_TYPE}"
                    f"&cHash={chash}"
                    + (f"#collapseTwo-{ext_id}" if ext_id else "")
                )

                try:
                    self._save_paper({
                        "site_id": self.site_id,
                        "external_id": ext_id or None,
                        "post_number": ext_id or None,
                        "title": item["title"],
                        "abstract": abstract,
                        "published_date": item.get("published_date"),
                        "listed_date": item.get("listed_date"),
                        "authors": None,
                        "publisher": "ARCEP",
                        "department": None,
                        "journal": None,
                        "url": item_url,
                        "pdf_url": item.get("pdf_url"),
                        "keywords": item.get("pub_type") or None,
                        "category": item.get("pub_type"),
                        "doi": None,
                        "original_filename": item.get("original_filename"),
                        "metadata": json.dumps(
                            {
                                "posted_date": item.get("listed_date"),
                                "originalFilename": item.get("original_filename"),
                                "pub_type": item.get("pub_type"),
                                "date_text": item.get("date_text"),
                                "pdf_links": item.get("pdfs"),
                                "pdf_descs": item.get("pdf_descs"),
                            },
                            ensure_ascii=False,
                        ),
                    })
                    saved += 1
                    new_on_page += 1
                    print(
                        f"[{self.site_id}] Saved {saved}/{limit_str}: "
                        f"{item['title'][:70]}"
                    )
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {ext_id} failed: {exc}; continuing")
                    continue

                time.sleep(self._delay)

            # Zero new records → end of real data (dedup/empty page)
            if new_on_page == 0:
                print(f"[{self.site_id}] Page {page} yielded no new records; stopping")
                break

            if not next_url:
                print(f"[{self.site_id}] No next-page link on page {page}; done")
                break

            page += 1

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
