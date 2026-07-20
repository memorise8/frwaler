# -*- coding: utf-8 -*-
"""Federal Department of Finance (efd.admin.ch) English news crawler.

Starting URL: https://www.efd.admin.ch/en/overview/nsb?sort=dateDecreasing&display=list&organization=601&topic=all&from=2024-08-29&to=2025-08-29

The list page is a Nuxt SPA that (per captured network traffic) loads its
results from a same-origin JSON feed:

    https://www.efd.admin.ch/ne/nsb/channel?lang=en&contentType=nsb-page&organisation=601&from=<YYYY-MM-DD>&to=<YYYY-MM-DD>

That endpoint ignores explicit limit/offset params and instead returns up
to ~500 items (newest first) within the given ``[from, to]`` window. We
paginate by repeatedly narrowing ``to`` to the oldest item's date from the
previous batch ("date-window walking") until a batch contributes no new
items or the beginning of the org's history is reached.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import time
from datetime import date
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler

try:
    from bs4 import BeautifulSoup as _BS
    _PARSERS = ["html5lib", "lxml", "html.parser"]
except ImportError:
    _BS = None
    _PARSERS = []


def _make_soup(raw_html: str):
    if _BS is None:
        raise RuntimeError("BeautifulSoup (bs4) is not installed")
    last_exc = None
    for parser in _PARSERS:
        try:
            return _BS(raw_html, parser)
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(f"No working HTML parser (tried {_PARSERS}): {last_exc}")


class EfdAdminChEnCrawler(BaseCrawler):
    """Crawler for the Swiss Federal Department of Finance news feed (efd.admin.ch)."""

    site_id = "efd-admin-ch-en"
    site_name = "Custom: efd-admin-ch-en"
    base_url = "https://www.efd.admin.ch"

    _API_URL = "https://www.efd.admin.ch/ne/nsb/channel"
    _ORG_API_URL = "https://www.efd.admin.ch/ne/nsb/organisations"
    _ORG_ID = "601"
    _CONTENT_TYPE = "nsb-page"
    _EARLY_FROM = "1990-01-01"
    _FALLBACK_PUBLISHER = "Federal Department of Finance"

    _MIN_ABSTRACT = 50
    _MAX_PAGES = 200
    _MAX_WALL = 25 * 60  # seconds

    # ------------------------------------------------------------------
    # curl helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url: str, params: dict | None = None) -> str | None:
        """GET via curl with exponential-backoff retries. Returns decoded text or None."""
        full_url = url if not params else f"{url}?{urlencode(params)}"
        cmd = [
            "curl", "-skL", "--tls-max", "1.3", "--max-time", "30",
            "-H", f"User-Agent: {self.USER_AGENT}",
            full_url,
        ]
        waits = [1, 3, 9]
        for attempt in range(3):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < 2:
                    print(f"[{self.site_id}] empty response, retry in {waits[attempt]}s: {full_url}")
                    time.sleep(waits[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(f"[{self.site_id}] curl error ({exc}), retry in {waits[attempt]}s: {full_url}")
                    time.sleep(waits[attempt])
                else:
                    print(f"[{self.site_id}] curl failed after 3 attempts: {exc}")
        return None

    def _curl_get_json(self, url: str, params: dict | None = None) -> dict | None:
        raw = self._curl_get(url, params)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"[{self.site_id}] JSON decode error for {url}: {exc}")
            return None

    # ------------------------------------------------------------------
    # Metadata helpers
    # ------------------------------------------------------------------

    def _fetch_publisher_name(self) -> str:
        """Best-effort lookup of the organisation's display name; falls back to a constant."""
        try:
            orgs = self._curl_get_json(self._ORG_API_URL, {"lang": "en"})
            if isinstance(orgs, list):
                for org in orgs:
                    if isinstance(org, dict) and str(org.get("id")) == self._ORG_ID:
                        name = org.get("name")
                        if name:
                            return name
        except Exception as exc:
            print(f"[{self.site_id}] organisation lookup failed: {exc}")
        return self._FALLBACK_PUBLISHER

    def _fetch_detail_enrichment(self, url: str) -> dict:
        """Best-effort extraction of a richer abstract / PDF link from the detail page.

        Never raises — returns an empty dict when nothing useful is found or
        the page can't be fetched/parsed.
        """
        result: dict = {}
        raw = self._curl_get(url)
        if not raw:
            return result
        try:
            soup = _make_soup(raw)
        except Exception as exc:
            print(f"[{self.site_id}] detail HTML parse error for {url}: {exc}")
            return result

        try:
            for attr, val in (("property", "og:description"), ("name", "description"), ("name", "twitter:description")):
                tag = soup.find("meta", attrs={attr: val})
                if tag and tag.get("content"):
                    text = html.unescape(tag["content"].strip())
                    if text:
                        result["abstract"] = text
                        break

            pdf_link = soup.find("a", href=re.compile(r"\.pdf($|\?)", re.I))
            if pdf_link and pdf_link.get("href"):
                href = pdf_link["href"].strip()
                if href.startswith("/"):
                    href = self.base_url + href
                result["pdf_url"] = href
                tail = href.rstrip("/").split("/")[-1].split("?")[0].split("#")[0]
                if tail and "." in tail:
                    result["original_filename"] = tail
        except Exception as exc:
            print(f"[{self.site_id}] detail parse error for {url}: {exc}")

        return result

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_urls: set = set()
        start_time = time.time()
        page = 0
        to_date = date.today().isoformat()

        publisher_name = self._fetch_publisher_name()

        try:
            while True:
                page += 1
                lim_str = str(limit) if limit is not None else "inf"

                if page > self._MAX_PAGES:
                    print(f"[{self.site_id}] Safety cap of {self._MAX_PAGES} pages reached. Stopping.")
                    break
                if time.time() - start_time > self._MAX_WALL:
                    print(f"[{self.site_id}] Wall-clock budget exceeded. Stopping cleanly.")
                    break

                data = self._curl_get_json(
                    self._API_URL,
                    {
                        "lang": "en",
                        "contentType": self._CONTENT_TYPE,
                        "organisation": self._ORG_ID,
                        "from": self._EARLY_FROM,
                        "to": to_date,
                    },
                )
                if not data:
                    print(f"[{self.site_id}] page {page}: list fetch failed. Stopping.")
                    break

                items = data.get("items") or []
                if not items:
                    print(f"[{self.site_id}] page {page}: 0 records returned. Stopping.")
                    break

                new_items = [it for it in items if it.get("link") and it["link"] not in seen_urls]
                if not new_items:
                    print(f"[{self.site_id}] page {page}: 0 new records (all seen). Stopping.")
                    break

                for item in new_items:
                    if limit is not None and saved >= limit:
                        break

                    link = item.get("link")
                    seen_urls.add(link)

                    try:
                        title = html.unescape((item.get("title") or "").strip())
                        if not title:
                            print(f"[{self.site_id}] item {link} has empty title, skipping.")
                            continue

                        feed_abstract = html.unescape((item.get("description") or "").strip())
                        published_date = item.get("publishedDate") or ""
                        external_id = item.get("id") or ""

                        time.sleep(self._delay)
                        enrichment = self._fetch_detail_enrichment(link)

                        abstract = enrichment.get("abstract") or ""
                        if len(abstract) < len(feed_abstract):
                            abstract = feed_abstract

                        if len(abstract) < self._MIN_ABSTRACT:
                            print(f"[{self.site_id}] short abstract ({len(abstract)}) for '{title[:50]}', skipping.")
                            continue

                        pdf_url = enrichment.get("pdf_url")
                        original_filename = enrichment.get("original_filename")

                        meta = {
                            "posted_date": published_date,
                            "efd_item_id": external_id,
                            "organisation_id": self._ORG_ID,
                        }
                        if item.get("author"):
                            meta["feed_author"] = item["author"]
                        if original_filename:
                            meta["originalFilename"] = original_filename

                        paper = {
                            "id": None,
                            "site_id": self.site_id,
                            "external_id": external_id,
                            "post_number": external_id,
                            "title": title,
                            "abstract": abstract,
                            "published_date": published_date,
                            "listed_date": published_date,
                            "authors": None,
                            "publisher": publisher_name,
                            "journal": None,
                            "url": link,
                            "pdf_url": pdf_url,
                            "keywords": None,
                            "category": None,
                            "doi": None,
                            "original_filename": original_filename,
                            "metadata": json.dumps(meta, ensure_ascii=False),
                        }

                        self._save_paper(paper)
                        saved += 1
                        print(f"[{self.site_id}] Saved {saved}/{lim_str}: {title[:60]}")

                    except Exception as exc:
                        print(f"[{self.site_id}] item {link} failed: {exc}; continuing.")
                        continue

                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{lim_str}")

                if limit is not None and saved >= limit:
                    break

                oldest_date = items[-1].get("publishedDate") or to_date
                if oldest_date >= to_date:
                    print(f"[{self.site_id}] page {page}: date window did not shrink. Stopping.")
                    break
                to_date = oldest_date

        except KeyboardInterrupt:
            print(f"[{self.site_id}] Interrupted by user. Saved {saved} so far.")
            raise

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
