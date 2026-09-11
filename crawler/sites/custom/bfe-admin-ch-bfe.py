# -*- coding: utf-8 -*-
"""BFE publications database crawler.

Fetch the live pubdb.bfe.admin.ch search directly, with page=N pagination.
The historical bfe.admin.ch exturl wrapper was removed during site migration;
its link decoder remains for compatibility with older listing fragments.
Abstracts describe observed list metadata, not extracted PDF body text.
"""

import base64
import json
import os
import re
import subprocess
import sys
import time
from urllib.parse import unquote, urljoin

sys.path.insert(0, ".")
from crawler.base_crawler import BaseCrawler  # absolute import — spec_from_file_location


_SEARCH_BASE = "https://pubdb.bfe.admin.ch/de/suche"
_EXTURL_PREFIX = "https://www.bfe.admin.ch/bfe/de/home/news-und-medien/publikationen.exturl.html/"

_LANG_NAMES = {"DE": "Deutsch", "FR": "Französisch", "IT": "Italienisch", "EN": "Englisch"}

_NOT_FOUND_TEXT = "Es wurden keine Publikationen gefunden."


class BfeAdminChBfeCrawler(BaseCrawler):
    """Crawler for the BFE (Bundesamt für Energie) publications database."""

    site_id = "bfe-admin-ch-bfe"
    site_name = "Custom: bfe-admin-ch-bfe"
    base_url = "https://www.bfe.admin.ch"

    _MIN_ABSTRACT = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _MAX_WALL = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))  # seconds
    _RETRY_WAITS = (1, 3, 9)

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url):
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    wait = self._RETRY_WAITS[attempt]
                    print(f"[{self.site_id}] empty response, retry in {wait}s: {url}")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = self._RETRY_WAITS[attempt]
                    print(f"[{self.site_id}] curl error ({exc}), retry in {wait}s: {url}")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl GET failed after 3 attempts: {exc}")
        return None

    def _curl_head_content_disposition(self, url):
        """HEAD via curl with retries. Returns the raw Content-Disposition header value or None."""
        cmd = [
            "curl", "-skI", "--tls-max", "1.3", "--max-time", "20",
            "-H", f"User-Agent: {self.USER_AGENT}",
            url,
        ]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=25)
                headers = result.stdout.decode("utf-8", errors="replace")
                match = re.search(r"^Content-Disposition:\s*(.+)$", headers, re.IGNORECASE | re.MULTILINE)
                if match:
                    return match.group(1).strip()
                # Successful HEAD with no Content-Disposition header — nothing to retry for.
                if re.search(r"^HTTP/\S+\s+2\d\d", headers, re.MULTILINE):
                    return None
                if attempt < 2:
                    wait = self._RETRY_WAITS[attempt]
                    print(f"[{self.site_id}] HEAD non-2xx, retry in {wait}s: {url}")
                    time.sleep(wait)
            except Exception as exc:
                if attempt < 2:
                    wait = self._RETRY_WAITS[attempt]
                    print(f"[{self.site_id}] HEAD error ({exc}), retry in {wait}s: {url}")
                    time.sleep(wait)
                else:
                    print(f"[{self.site_id}] curl HEAD failed after 3 attempts: {exc}")
        return None

    # ------------------------------------------------------------------
    # exturl base64 helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _build_page_url(page_num):
        """Use the live publication database; the old BFE wrapper was removed."""
        return f"{_SEARCH_BASE}?page={page_num}&x=1"

    @staticmethod
    def _decode_exturl_href(href):
        """Decode an exturl-wrapped href (harvested from page HTML) to the real target URL.

        Some hrefs found in the site's own HTML contain a literal embedded ``/``
        inside the base64 segment (an app rendering quirk), so we strip all
        ``/`` out of the segment before decoding and re-pad to a multiple of 4.
        """
        try:
            marker = ".exturl.html/"
            idx = href.find(marker)
            if idx == -1:
                if re.match(r"^/(de|fr|it|en)/publication/download/\d+", href):
                    return urljoin(_SEARCH_BASE, href)
                return None
            seg = href[idx + len(marker):]
            seg = seg.rsplit(".html", 1)[0]
            seg = seg.split("?", 1)[0]
            seg = seg.replace("/", "")
            pad = (-len(seg)) % 4
            seg = seg + "=" * pad
            decoded = base64.urlsafe_b64decode(seg).decode("utf-8", errors="replace")
            return decoded
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _make_soup(self, html_text):
        """Try html5lib → lxml → html.parser; returns BeautifulSoup or None."""
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html_text, parser)
            except Exception:
                continue
        return None

    @staticmethod
    def _to_iso_date(ddmmyyyy):
        """Convert 'DD.MM.YYYY' to ISO 'YYYY-MM-DD'. Returns None if unparseable."""
        if not ddmmyyyy:
            return None
        m = re.match(r"^\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*$", ddmmyyyy)
        if not m:
            return None
        day, month, year = m.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"

    def _parse_list_page(self, html_text):
        """Parse a search-results page into a list of item dicts.

        Returns ``(items, not_found)`` where ``not_found`` is True if the page
        signals the end of pagination.
        """
        if _NOT_FOUND_TEXT in html_text:
            return [], True

        try:
            soup = self._make_soup(html_text)
        except Exception as exc:
            print(f"[{self.site_id}] HTML parse error: {exc}")
            return [], False

        if soup is None:
            return [], False

        items = []
        divs = soup.find_all("div", class_="list-group-item")
        if not divs:
            raise ValueError("BFE response has neither publication records nor an explicit empty-results message")

        for div in divs:
            try:
                strong = div.find("strong")
                title = strong.get_text(strip=True) if strong else ""

                p_tag = div.find("p")
                p_text = p_tag.get_text(" ", strip=True) if p_tag else ""

                erschienen_m = re.search(r"Erschienen:\s*([\d.]+)", p_text)
                published_raw = erschienen_m.group(1).strip() if erschienen_m else ""

                filetype_m = re.search(r"Dateityp:\s*(\S+)", p_text)
                filetype = filetype_m.group(1).strip() if filetype_m else ""

                size_m = re.search(r"Größe:\s*([\d.,]+\s*\S+)", p_text)
                size_str = size_m.group(1).strip() if size_m else ""

                pub_id = ""
                span = div.find("span", string=re.compile(r"ID:\s*\d+"))
                if span:
                    id_m = re.search(r"ID:\s*(\d+)", span.get_text())
                    if id_m:
                        pub_id = id_m.group(1)

                lang_links = {}
                if p_tag:
                    for a in p_tag.find_all("a", href=True):
                        lang_code = a.get_text(strip=True).upper()
                        if lang_code in _LANG_NAMES:
                            lang_links[lang_code] = a["href"]

                items.append({
                    "title": title,
                    "published_raw": published_raw,
                    "filetype": filetype,
                    "size_str": size_str,
                    "pub_id": pub_id,
                    "lang_links": lang_links,
                })
            except Exception as exc:
                print(f"[{self.site_id}] item div parse error: {exc}")
                continue

        return items, False

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl BFE publications database, page by page, from the search results list."""
        saved = 0
        seen_ids = set()
        start_time = time.time()
        page_num = 0

        try:
            for page_idx in range(1, self._MAX_PAGES + 1):
                page_num = page_idx

                if limit is not None and saved >= limit:
                    break

                if page_idx > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping "
                          f"(note: the real site has ~851 pages, so limit=None will not reach the "
                          f"full corpus under this cap).")
                    break

                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                page_url = self._build_page_url(page_idx)
                raw = self._curl_get(page_url)
                if not raw:
                    print(f"[{self.site_id}] page {page_idx}: fetch failed after retries. Stopping.")
                    break

                items, not_found = self._parse_list_page(raw)

                if not_found or not items:
                    print(f"[{self.site_id}] page {page_idx}: no more items "
                          f"({'keine Publikationen gefunden' if not_found else 'empty page'}). Done.")
                    break

                if page_idx % 10 == 0 or page_idx == 1:
                    lim_str = str(limit) if limit is not None else "inf"
                    print(f"[{self.site_id}] page {page_idx}: saved {saved}/{lim_str}")

                for item in items:
                    if limit is not None and saved >= limit:
                        break

                    if time.time() - start_time > self._MAX_WALL:
                        print(f"[{self.site_id}] Wall-clock budget exceeded mid-page. Stopping cleanly.")
                        return saved

                    pub_id = item["pub_id"]
                    title = item["title"]

                    try:
                        if not pub_id:
                            print(f"[{self.site_id}] Skipping item with no pub_id: '{title[:50]}'")
                            continue

                        if pub_id in seen_ids:
                            continue
                        seen_ids.add(pub_id)

                        if not title:
                            print(f"[{self.site_id}] Skipping item with no title (id={pub_id})")
                            continue

                        lang_links = item["lang_links"]
                        if not lang_links:
                            print(f"[{self.site_id}] Skipping '{title[:50]}' (id={pub_id}): no download links")
                            continue

                        # Prefer DE, else whichever language link is present first.
                        chosen_lang = "DE" if "DE" in lang_links else next(iter(lang_links))
                        chosen_href = lang_links[chosen_lang]

                        pdf_url = self._decode_exturl_href(chosen_href)
                        original_url = pdf_url
                        if not pdf_url:
                            print(f"[{self.site_id}] Skipping '{title[:50]}' (id={pub_id}): could not decode download link")
                            continue

                        published_iso = self._to_iso_date(item["published_raw"])
                        available_langs = sorted(lang_links.keys())

                        abstract = (
                            f"{title}. Publikation des Bundesamts für Energie (BFE). "
                            f"Erschienen: {published_iso or item['published_raw']}. "
                            f"Dateityp: {item['filetype']}. Größe: {item['size_str']}. "
                            f"Verfügbare Sprachversionen: {', '.join(available_langs)}. "
                            f"Publikations-ID: {pub_id}."
                        )
                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] Short abstract ({len(abstract)}) for '{title[:50]}', skipping.")
                            continue

                        # Rate-limit before the per-item HEAD request.
                        time.sleep(self._delay)
                        original_filename = None
                        cd_header = self._curl_head_content_disposition(pdf_url)
                        if cd_header:
                            star_m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", cd_header, re.IGNORECASE)
                            if star_m:
                                original_filename = unquote(star_m.group(1).strip())
                            else:
                                plain_m = re.search(r'filename\s*=\s*"?([^";]+)"?', cd_header, re.IGNORECASE)
                                if plain_m:
                                    original_filename = plain_m.group(1).strip()
                        if not original_filename:
                            tail = pdf_url.rstrip("/").split("/")[-1]
                            original_filename = tail or None

                        metadata = {
                            "posted_date": item["published_raw"],
                            "originalFilename": original_filename,
                            "pub_id": pub_id,
                            "filetype": item["filetype"],
                            "size_str": item["size_str"],
                            "available_languages": available_langs,
                            "chosen_language": chosen_lang,
                        }

                        paper = {
                            "site_id": self.site_id,
                            "external_id": pub_id,
                            "post_number": pub_id,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_iso,
                            "posted_date": published_iso,
                            "publisher": "Bundesamt für Energie BFE",
                            "url": original_url,
                            "pdf_url": pdf_url,
                            "original_filename": original_filename,
                            "category": "Publikation",
                            "keywords": "",
                            "doi": "",
                            "authors": "",
                            "journal": "",
                            "metadata": json.dumps(metadata, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        lim_str = str(limit) if limit is not None else "inf"
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        print(f"[{self.site_id}] item {pub_id or '?'} failed: {exc}; continuing.")
                        continue

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved} (pages walked: {page_num})")
        return saved
