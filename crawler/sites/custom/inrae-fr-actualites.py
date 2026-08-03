# -*- coding: utf-8 -*-
"""Crawler for INRAE actualites (CP National press releases) via Algolia API."""

from __future__ import annotations

import os
import json
import re
import subprocess
import time
import unicodedata
from datetime import datetime, timezone
from html import unescape
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from crawler.base_crawler import BaseCrawler


class InraeFrActualitesCrawler(BaseCrawler):
    site_id = "inrae-fr-actualites"
    site_name = "Custom: inrae-fr-actualites"
    base_url = "https://www.inrae.fr"

    ALGOLIA_APP_ID = "DVUTVWXJFU"
    ALGOLIA_API_KEY = os.environ.get("INRAE_FR_ACTUALITES_KEY", "")
    ALGOLIA_INDEX = "inrae_prod_created_date_desc"
    ALGOLIA_ENDPOINT = (
        "https://DVUTVWXJFU-dsn.algolia.net/1/indexes/"
        "inrae_prod_created_date_desc/query"
    )
    ALGOLIA_FILTER = 'field_system_tags_name:"CP National"'
    HITS_PER_PAGE = 20

    BACKOFF_SECONDS = (1, 3, 9)
    CURL_TIMEOUT = 45
    MIN_ABSTRACT_CHARS = 50    # skip items below this
    MIN_SAVE_ABSTRACT_CHARS = 100  # test requirement; try detail page if below
    MAX_PAGES = 200
    MAX_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))

    MONTHS_FR = {
        "janvier": 1, "fevrier": 2, "mars": 3, "avril": 4,
        "mai": 5, "juin": 6, "juillet": 7, "aout": 8,
        "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    }

    def __init__(self, db_conn, delay=1.0, detail_delay=None):
        super().__init__(db_conn=db_conn, delay=delay)
        self.detail_delay = delay if detail_delay is None else detail_delay

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl INRAE CP National press releases via Algolia search API."""
        saved = 0
        page = 0
        seen_urls = set()
        limit_str = str(limit) if limit is not None else "inf"
        start_time = time.time()

        while True:
            if limit is not None and saved >= limit:
                break
            if page >= self.MAX_PAGES:
                print(f"[{self.site_id}] safety cap of {self.MAX_PAGES} pages reached; stopping")
                break
            if time.time() - start_time > self.MAX_SECONDS:
                print(f"[{self.site_id}] 25-minute budget exhausted; stopping cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            hits, nb_pages = self._algolia_page(page)
            if hits is None:
                print(f"[{self.site_id}] Algolia query failed at page {page}; stopping")
                break
            if not hits:
                print(f"[{self.site_id}] page {page}: no hits; stopping")
                break

            new_hits = [h for h in hits if self._hit_url(h) not in seen_urls]
            if not new_hits:
                print(f"[{self.site_id}] page {page}: all items already seen; stopping")
                break

            for hit in new_hits:
                if limit is not None and saved >= limit:
                    break

                nid = hit.get("nid") or "?"
                try:
                    detail_url = self._hit_url(hit)
                    if not detail_url or detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)

                    abstract = self._extract_algolia_abstract(hit)

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] nid={nid} skipped: "
                            f"Algolia intro too short ({len(abstract)} chars); "
                            "fetching detail page"
                        )
                        time.sleep(self.detail_delay)
                        abstract = self._fetch_detail_abstract(detail_url) or ""

                    if len(abstract) < self.MIN_ABSTRACT_CHARS:
                        print(
                            f"[{self.site_id}] nid={nid} skipped: "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    # Fetch detail page only when abstract is still borderline
                    if self.MIN_ABSTRACT_CHARS <= len(abstract) < self.MIN_SAVE_ABSTRACT_CHARS:
                        time.sleep(self.detail_delay)
                        longer = self._fetch_detail_abstract(detail_url) or ""
                        if len(longer) > len(abstract):
                            abstract = longer

                    pub_date = self._unix_to_iso(hit.get("created"))
                    upd_date = self._unix_to_iso(hit.get("field_update_date"))
                    tags = hit.get("field_tags_name") or []
                    sys_tags = hit.get("field_system_tags_name") or []
                    all_keywords = tags + [t for t in sys_tags if t not in tags]

                    paper = {
                        "site_id": self.site_id,
                        "external_id": str(nid),
                        "post_number": str(nid),
                        "title": self._clean_text(hit.get("title") or ""),
                        "abstract": abstract,
                        "published_date": pub_date,
                        "listed_date": pub_date,
                        "url": detail_url,
                        "pdf_url": None,
                        "keywords": ", ".join(tags),
                        "category": self._clean_text(
                            hit.get("label_content_type")
                            or hit.get("field_thematic_name")
                            or ""
                        ),
                        "department": self._clean_text(hit.get("name_department") or ""),
                        "publisher": "INRAE",
                        "authors": None,
                        "doi": None,
                        "original_filename": None,
                        "metadata": json.dumps(
                            {
                                "node_id": nid,
                                "nid": nid,
                                "objectID": hit.get("objectID"),
                                "field_thematic_name": hit.get("field_thematic_name"),
                                "label_content_type": hit.get("label_content_type"),
                                "field_system_tags_name": sys_tags,
                                "field_tags_name": tags,
                                "title_center": hit.get("title_center"),
                                "name_department": hit.get("name_department"),
                                "created": hit.get("created"),
                                "field_update_date": hit.get("field_update_date"),
                                "updated_date": upd_date,
                                "posted_date": pub_date,
                                "originalFilename": None,
                            },
                            ensure_ascii=False,
                        ),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(
                        f"[{self.site_id}] saved {saved}/{limit_str}: "
                        f"{paper['title'][:80]}"
                    )

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] nid={nid} failed: {exc}")
                    continue

            if nb_pages is not None and page >= nb_pages - 1:
                print(f"[{self.site_id}] reached last page ({nb_pages}); stopping")
                break

            page += 1

        print(f"[{self.site_id}] done. Total saved: {saved}")
        return saved

    # ------------------------------------------------------------------
    # Algolia helpers
    # ------------------------------------------------------------------

    def _algolia_page(self, page):
        """Query Algolia; returns (hits_list, nb_pages) or (None, None) on error."""
        payload = json.dumps(
            {
                "query": "",
                "filters": self.ALGOLIA_FILTER,
                "hitsPerPage": self.HITS_PER_PAGE,
                "page": page,
            }
        )
        raw = self._curl_post(
            self.ALGOLIA_ENDPOINT,
            data=payload,
            headers={
                "X-Algolia-Application-Id": self.ALGOLIA_APP_ID,
                "X-Algolia-API-Key": self.ALGOLIA_API_KEY,
                "Content-Type": "application/json",
            },
            context=f"algolia page {page}",
        )
        if not raw:
            return None, None
        try:
            data = json.loads(raw)
            return data.get("hits", []), data.get("nbPages")
        except (json.JSONDecodeError, Exception) as exc:
            print(f"[{self.site_id}] Algolia JSON parse error at page {page}: {exc}")
            return None, None

    def _hit_url(self, hit):
        """Return absolute detail URL for a hit."""
        rel = (hit.get("url") or "").strip()
        if not rel:
            return ""
        return urljoin(self.base_url, rel)

    def _extract_algolia_abstract(self, hit):
        """Extract abstract from the rendered_item_listing HTML (Teaser-intro)."""
        html_snip = hit.get("rendered_item_listing") or ""
        if not html_snip:
            return ""
        soup = self._make_soup(html_snip, context="rendered_item_listing")
        if soup is None:
            return ""
        intro = soup.select_one(".Teaser-intro")
        if intro:
            return self._clean_multiline(intro.get_text(" ", strip=True))
        # Fallback: any long paragraph
        for p in soup.find_all("p"):
            text = self._clean_multiline(p.get_text(" ", strip=True))
            if len(text) >= self.MIN_ABSTRACT_CHARS:
                return text
        return ""

    # ------------------------------------------------------------------
    # Detail page fetch (used when Algolia intro is short)
    # ------------------------------------------------------------------

    def _fetch_detail_abstract(self, url):
        """Fetch detail page and extract full article text as abstract."""
        raw = self._curl_get(url, context="detail page")
        if not raw:
            return ""
        soup = self._make_soup(raw, context=f"detail {url}")
        if soup is None:
            return ""

        article = soup.select_one("article") or soup
        # Remove nav/header/footer noise
        for bad in article.select(
            "script, style, noscript, header, footer, nav, "
            ".Teaser, .Share, .fr-share, .breadcrumb, "
            ".visually-hidden, .sr-only"
        ):
            bad.decompose()

        parts = []
        for node in article.find_all(["p", "li", "h2", "h3"], recursive=True):
            text = self._clean_multiline(node.get_text(" ", strip=True))
            if len(text) >= 40 and text not in parts:
                parts.append(text)

        return "\n\n".join(parts[:20])  # first 20 paragraphs is plenty

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_post(self, url, data, headers=None, context="POST"):
        """HTTP POST via curl; returns response body string or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--compressed",
            "--connect-timeout", "15", "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-X", "POST",
            "-d", data,
        ]
        for k, v in (headers or {}).items():
            cmd.extend(["-H", f"{k}: {v}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} POST attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} POST failed after 3 attempts: {last_error}")
        return None

    def _curl_get(self, url, context="GET", referer=None):
        """HTTP GET via curl; returns response body string or None."""
        cmd = [
            "curl", "--tls-max", "1.3", "-skL", "--compressed",
            "--connect-timeout", "15", "--max-time", str(self.CURL_TIMEOUT),
            "-A", self.USER_AGENT,
            "-H", "Accept: text/html,application/xhtml+xml,*/*;q=0.8",
            "-H", "Accept-Language: fr-FR,fr;q=0.9,en;q=0.7",
        ]
        if referer:
            cmd.extend(["-H", f"Referer: {referer}"])
        cmd.append(url)

        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=self.CURL_TIMEOUT + 10
                )
                body = (result.stdout or b"").decode("utf-8", errors="replace")
                if result.returncode != 0:
                    stderr = (result.stderr or b"").decode("utf-8", errors="replace").strip()
                    raise RuntimeError(stderr or f"curl exit {result.returncode}")
                if not body.strip():
                    raise RuntimeError("empty response")
                return body
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] {context} GET attempt {attempt}/3 failed: {last_error}")
                if attempt < 3:
                    wait = self.BACKOFF_SECONDS[attempt - 1]
                    time.sleep(wait)

        print(f"[{self.site_id}] {context} GET failed after 3 attempts: {last_error}")
        return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, raw, context="HTML"):
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        text = raw or ""
        last_exc = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                return BeautifulSoup(text, parser)
            except Exception as exc:
                last_exc = exc
                print(f"[{self.site_id}] BeautifulSoup({parser}) failed for {context}: {exc}")
        print(f"[{self.site_id}] all parsers failed for {context}: {last_exc}")
        return None

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _unix_to_iso(ts):
        """Convert Unix timestamp (int/float) to ISO date string YYYY-MM-DD."""
        if not ts:
            return None
        try:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            return dt.strftime("%Y-%m-%d")
        except (ValueError, OSError, OverflowError):
            return None

    @staticmethod
    def _clean_text(value):
        text = unescape(str(value or ""))
        text = text.replace("\xa0", " ").replace("​", "")
        text = re.sub(r"[ \t\r\f\v]+", " ", text)
        return text.strip()

    @classmethod
    def _clean_multiline(cls, value):
        text = cls._clean_text(value)
        text = re.sub(r" *\n+ *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
