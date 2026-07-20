# -*- coding: utf-8 -*-
"""BMLEH (Bundesministerium fuer Landwirtschaft, Ernaehrung und Heimat) Gesetzestexte crawler.

Starting URL:
    https://www.bmleh.de/SiteGlobals/Forms/Suche/DE/Gesetzestexte/Gesetzestexte_Formular.html

This is a "SiteGlobals" search form typical of German federal ministry sites
(a shared Government-CMS "Baukasten" framework used across many *.bund.de /
*.de ministry sites). The search results are rendered server-side as plain
HTML (no separate JSON API). Pagination works via a `gtp=<listId>_list%253D<N>`
query parameter -- the numeric `<listId>` is tied to this particular search
form/list instance and is **discovered dynamically** from the page's own
markup on every crawl run rather than hard-coded (it happened to be stable
("42726") across independent test fetches, but nothing guarantees that going
forward). `resultsPerPage=50` (the largest page size offered by the UI) is
used to minimize the number of list-page fetches; the site echoes this
choice back into every subsequent pagination link.

Detail pages ("Gesetzestexte") have no free-text body -- each page instead
lists one or more PDF download links grouped by section (Kabinettfassung /
Referentenentwurf / Stellungnahmen / ...), plus a shared boilerplate
paragraph and a keyword list. The abstract is synthesized from the
boilerplate + the document-section summary + keywords so it stays
meaningful and comfortably above the minimum length while remaining
faithful to real page content (no fabricated text).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from urllib.parse import urljoin

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


def _clean_text(text) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", text).strip()


class BmlehDeSiteglobalsCrawler(BaseCrawler):
    """Crawler for BMLEH (bmleh.de) Gesetzestexte (draft laws / regulations) search."""

    site_id = "bmleh-de-siteglobals"
    site_name = "Custom: bmleh-de-siteglobals"
    base_url = "https://www.bmleh.de"

    _START_URL = (
        "https://www.bmleh.de/SiteGlobals/Forms/Suche/DE/Gesetzestexte/"
        "Gesetzestexte_Formular.html"
    )
    _RESULTS_PER_PAGE = 50
    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds
    _PUBLISHER = "Bundesministerium für Landwirtschaft, Ernährung und Heimat (BMLEH)"

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str) -> str | None:
        """GET via curl (TLS 1.3 max, cert checks disabled) with 1s/3s/9s backoff retries."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                try:
                    text = result.stdout.decode("utf-8")
                except UnicodeDecodeError:
                    text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _fetch(self, url: str) -> str | None:
        """Fetch a URL: prefer ``self._request`` (built-in retry/backoff/rate-limit);
        fall back to a curl subprocess (TLS-max 1.3) if that fails, since these
        German government sites sometimes reject the plain `requests` TLS handshake.
        """
        resp = None
        try:
            resp = self._request(url)
        except Exception as exc:
            print(f"[{self.site_id}] _request raised for {url}: {exc}")
            resp = None

        if resp is not None:
            try:
                return resp.content.decode("utf-8")
            except UnicodeDecodeError:
                return resp.content.decode("utf-8", errors="replace")

        print(f"[{self.site_id}] _request failed for {url}, falling back to curl")
        return self._curl_get(url)

    # ------------------------------------------------------------------
    # List-page parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _discover_list_id(raw_html: str) -> str | None:
        m = re.search(r"gtp=(\d+)_list", raw_html)
        return m.group(1) if m else None

    @staticmethod
    def _total_results(soup) -> int | None:
        p = soup.find("p", class_="c-search-resultsperpage__p")
        if not p:
            return None
        m = re.search(r"von\s+(\d+)", p.get_text())
        return int(m.group(1)) if m else None

    def _parse_list_items(self, soup) -> list:
        items = []
        for teaser in soup.find_all("div", class_="c-searchteaser"):
            try:
                h2 = teaser.find("h2", class_="c-searchteaser__h")
                if not h2:
                    continue
                a = h2.find("a", href=True)
                if not a:
                    continue
                href = (a.get("href") or "").strip()
                title = a.get_text(strip=True)
                if not href or not title:
                    continue

                listed_iso = ""
                category = ""
                meta_p = teaser.find("p", class_="c-searchteaser__meta")
                if meta_p:
                    time_tag = meta_p.find("time")
                    if time_tag is not None:
                        listed_iso = (time_tag.get("datetime") or "").strip()
                    for span in meta_p.find_all("span"):
                        if "aural" in (span.get("class") or []):
                            continue
                        t = _clean_text(span.get_text())
                        if t:
                            category = t

                items.append({
                    "href": href,
                    "title": title,
                    "listed_iso": listed_iso or None,
                    "category": category or None,
                })
            except Exception as exc:
                print(f"[{self.site_id}] teaser parse error: {exc}")
                continue
        return items

    # ------------------------------------------------------------------
    # Detail-page parsing
    # ------------------------------------------------------------------

    def _extract_detail(self, soup) -> dict:
        h1 = soup.find("h1", class_="c-intro__headline")
        title = _clean_text(h1.get_text()) if h1 is not None else ""

        published_iso = ""
        category = ""
        meta_p = soup.find("p", class_="c-teaser__meta")
        if meta_p:
            time_tag = meta_p.find("time")
            if time_tag is not None:
                published_iso = (time_tag.get("datetime") or "").strip()
            for span in meta_p.find_all("span"):
                if "aural" in (span.get("class") or []):
                    continue
                t = _clean_text(span.get_text())
                if t:
                    category = t

        intro_text = ""
        if h1 is not None:
            intro_div = h1.find_parent("div", class_="c-intro")
            sib = intro_div.find_next_sibling("p") if intro_div else None
            if sib is not None:
                intro_text = _clean_text(sib.get_text())

        keywords_list = []
        for a in soup.select("div.c-keywords ul.c-keywords__ul li a"):
            t = _clean_text(a.get_text())
            if t:
                keywords_list.append(t)

        # Document sections: a <div class="c-module-caption"><h2>Name</h2></div>
        # immediately followed by a <div class="c-linklist"> holding PDF links.
        documents = []
        for caption in soup.find_all("div", class_="c-module-caption"):
            h2 = caption.find("h2", class_="c-module-caption__h")
            section_name = _clean_text(h2.get_text()) if h2 is not None else ""
            if not section_name or section_name in ("Schlagworte", "Das könnte Sie auch interessieren"):
                continue
            linklist = caption.find_next_sibling("div", class_="c-linklist")
            if linklist is None:
                continue
            for a in linklist.find_all("a", href=True):
                href = a["href"].strip()
                if ".pdf" not in href.lower():
                    continue
                pdf_url = urljoin(self.base_url + "/", href)
                filename = pdf_url.split("?")[0].rstrip("/").split("/")[-1] or None
                documents.append({
                    "section": section_name,
                    "url": pdf_url,
                    "filename": filename,
                    "link_text": _clean_text(a.get_text()),
                })

        return {
            "title": title,
            "published_date": published_iso or None,
            "category": category or None,
            "intro_text": intro_text,
            "keywords": keywords_list,
            "documents": documents,
        }

    @staticmethod
    def _derive_ids(detail_url: str) -> tuple:
        """Derive (external_id, post_number) from the detail URL slug.

        Gesetzestexte slugs are descriptive (e.g. 'aend-tierarzneimittelgesetz'),
        not numeric, so post_number falls back to the slug itself unless a
        numeric prefix is present (e.g. '2-vo-aend-agrarstatistik-vo' -> '2').
        """
        slug = detail_url.rstrip("/").split("/")[-1]
        slug_noext = slug[:-5] if slug.lower().endswith(".html") else slug
        num_match = re.match(r"^(\d+)", slug_noext)
        if num_match:
            post_number = num_match.group(1)
        else:
            post_number = slug_noext or None
        external_id = slug_noext or None
        return external_id, post_number

    # ------------------------------------------------------------------
    # Per-item processing
    # ------------------------------------------------------------------

    def _process_item(self, item: dict, detail_url: str) -> bool:
        """Fetch + parse + save one item. Returns True if saved, False if skipped."""
        time.sleep(self._delay)

        raw = self._fetch(detail_url)
        if not raw:
            print(f"[{self.site_id}] failed to fetch detail page: {detail_url}")
            return False

        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error for {detail_url}: {exc}")
            return False

        detail = self._extract_detail(soup)

        title = detail["title"] or item["title"]
        if not title:
            print(f"[{self.site_id}] empty title for {detail_url}, skipping.")
            return False

        documents = detail["documents"]
        doc_summary = "; ".join(
            f"{d['section']}: {d['link_text'] or d['filename']}" for d in documents
        )
        keyword_str = ", ".join(detail["keywords"]) if detail["keywords"] else ""

        abstract_parts = []
        if detail["intro_text"]:
            abstract_parts.append(detail["intro_text"])
        if doc_summary:
            abstract_parts.append(f"Verfügbare Dokumente: {doc_summary}.")
        if keyword_str:
            abstract_parts.append(f"Schlagworte: {keyword_str}.")
        abstract = " ".join(abstract_parts).strip()

        if len(abstract) < self._MIN_ABSTRACT:
            print(f"[{self.site_id}] short abstract ({len(abstract)} chars) for '{title[:50]}', skipping.")
            return False

        published_date = detail["published_date"] or item["listed_iso"] or None
        listed_date = item["listed_iso"] or detail["published_date"] or None

        external_id, post_number = self._derive_ids(detail_url)

        primary_pdf = None
        for preferred in ("Kabinettfassung", "Referentenentwurf"):
            primary_pdf = next((d for d in documents if d["section"] == preferred), None)
            if primary_pdf is not None:
                break
        if primary_pdf is None and documents:
            primary_pdf = documents[0]

        meta = {
            "posted_date": item["listed_iso"] or "",
            "originalFilename": primary_pdf["filename"] if primary_pdf else None,
            "journal_raw": None,
            "series": None,
            "volume": None,
            "issue": None,
            "documents": documents,
        }

        paper = {
            "id": None,
            "site_id": self.site_id,
            "external_id": external_id,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "listed_date": listed_date,
            "authors": None,
            "publisher": self._PUBLISHER,
            "department": None,
            "journal": None,
            "url": detail_url,
            "pdf_url": primary_pdf["url"] if primary_pdf else None,
            "keywords": keyword_str or None,
            "category": detail.get("category") or item.get("category"),
            "doi": None,
            "original_filename": primary_pdf["filename"] if primary_pdf else None,
            "metadata": json.dumps(meta, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 1
        list_id = None
        total_results = None

        current_url = f"{self._START_URL}?resultsPerPage={self._RESULTS_PER_PAGE}"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                elapsed = time.time() - start_time
                if elapsed > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget ({self._MAX_WALL}s) exceeded. Stopping cleanly.")
                    break

                raw = self._fetch(current_url)
                if not raw:
                    print(f"[{self.site_id}] Failed to fetch list page {page} ({current_url}). Stopping.")
                    break

                try:
                    soup = _make_soup(raw)
                except Exception as exc:
                    print(f"[{self.site_id}] HTML parse error on list page {page}: {exc}. Stopping.")
                    break

                if list_id is None:
                    list_id = self._discover_list_id(raw)
                if total_results is None:
                    total_results = self._total_results(soup)

                items = self._parse_list_items(soup)
                if not items:
                    print(f"[{self.site_id}] Page {page}: no items found. Stopping.")
                    break

                new_count = 0
                for it in items:
                    if limit is not None and saved >= limit:
                        break

                    detail_url = urljoin(self.base_url + "/", it["href"]).split("#")[0]
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    new_count += 1

                    try:
                        if self._process_item(it, detail_url):
                            saved += 1
                            lim_str = str(limit) if limit is not None else "inf"
                            print(f"[{self.site_id}] Saved {saved}/{lim_str}: {it['title'][:60]}")
                    except Exception as exc:
                        print(f"[{self.site_id}] item {detail_url} failed: {exc}")
                        continue

                if page % 10 == 0:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                if limit is not None and saved >= limit:
                    break
                if new_count == 0:
                    print(f"[{self.site_id}] Page {page} yielded 0 new records (possible pagination loop). Stopping.")
                    break
                if list_id is None:
                    print(f"[{self.site_id}] Could not discover pagination id after page {page}. Stopping.")
                    break

                page += 1
                if total_results is not None and (page - 1) * self._RESULTS_PER_PAGE >= total_results:
                    print(f"[{self.site_id}] Reached last page ({total_results} total results). Stopping.")
                    break

                current_url = (
                    f"{self._START_URL}?gtp={list_id}_list%253D{page}"
                    f"&resultsPerPage={self._RESULTS_PER_PAGE}"
                )

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
