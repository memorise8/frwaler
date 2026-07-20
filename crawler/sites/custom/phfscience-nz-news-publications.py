# -*- coding: utf-8 -*-
"""PHF Science NZ corporate publications crawler.

Fetches all ``corporatePublicationItem`` records from the site's public
Algolia search index (app EE0122B7GY, index prod_esr).  For records whose
Algolia description is <100 chars the detail page is fetched to extract a
longer abstract.  Records that still have <50 chars are skipped.
"""

import os
import json
import re
import subprocess
import time
from datetime import datetime

from crawler.base_crawler import BaseCrawler

_ALGOLIA_APP_ID = "EE0122B7GY"
_ALGOLIA_API_KEY = os.environ.get("PHFSCIENCE_NZ_NEWS_PUBLICATIONS_KEY", "")
_ALGOLIA_INDEX = "prod_esr"
_ALGOLIA_URL = (
    f"https://{_ALGOLIA_APP_ID}-dsn.algolia.net/1/indexes/{_ALGOLIA_INDEX}/query"
)

# Boilerplate phrases from the PHF Science page template — filter these out
# when harvesting abstract text from detail pages.
_BOILERPLATE = (
    "PHF Science",
    "New Zealand Institute",
    "formerly named",
    "formerly the Institute",
    "Please note:",
    "Send us an enquiry",
    "Working in partnership",
    "©",  # copyright symbol
    "Dashboards display",
    "papatohu",
)


class PhfScienceNzNewsPublicationsCrawler(BaseCrawler):

    site_id = "phfscience-nz-news-publications"
    site_name = "Custom: phfscience-nz-news-publications"
    base_url = "https://www.phfscience.nz"

    # ------------------------------------------------------------------
    # Network helpers
    # ------------------------------------------------------------------

    def _curl_get(self, url, retries=3):
        """GET via curl; returns decoded text or None on failure."""
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-A", self.USER_AGENT,
            url,
        ]
        backoff = (1, 3, 9)
        for attempt in range(retries):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return text
                if attempt < retries - 1:
                    wait = backoff[attempt]
                    print(
                        f"[{self.site_id}] empty response from {url}, "
                        f"retrying in {wait}s…"
                    )
                    time.sleep(wait)
            except Exception as exc:
                if attempt < retries - 1:
                    wait = backoff[attempt]
                    print(
                        f"[{self.site_id}] curl error ({url}): {exc}, "
                        f"retrying in {wait}s…"
                    )
                    time.sleep(wait)
                else:
                    print(
                        f"[{self.site_id}] curl failed after {retries} "
                        f"attempts for {url}: {exc}"
                    )
        return None

    def _algolia_query(self, page=0, hits_per_page=50):
        """POST to Algolia; returns parsed JSON dict or None."""
        payload = json.dumps({
            "query": "",
            "facetFilters": [["contentTypeAlias:corporatePublicationItem"]],
            "hitsPerPage": hits_per_page,
            "page": page,
        })
        cmd = [
            "curl", "--tls-max", "1.3", "-sk", "--max-time", "30",
            "-X", "POST",
            "-H", f"X-Algolia-Application-Id: {_ALGOLIA_APP_ID}",
            "-H", f"X-Algolia-API-Key: {_ALGOLIA_API_KEY}",
            "-H", "Content-Type: application/json",
            "-d", payload,
            _ALGOLIA_URL,
        ]
        backoff = (1, 3, 9)
        for attempt in range(3):
            try:
                result = subprocess.run(
                    cmd, capture_output=True, timeout=35
                )
                text = result.stdout.decode("utf-8", errors="replace")
                if text.strip():
                    return json.loads(text)
                if attempt < 2:
                    time.sleep(backoff[attempt])
            except Exception as exc:
                if attempt < 2:
                    print(
                        f"[{self.site_id}] Algolia error (attempt "
                        f"{attempt+1}): {exc}, retrying…"
                    )
                    time.sleep(backoff[attempt])
                else:
                    print(f"[{self.site_id}] Algolia failed: {exc}")
        return None

    # ------------------------------------------------------------------
    # Abstract enrichment
    # ------------------------------------------------------------------

    def _extract_page_abstract(self, path):
        """Fetch the detail page and return the best paragraph text found."""
        url = (
            self.base_url + path
            if path.startswith("/")
            else path
        )
        html = self._curl_get(url)
        if not html:
            return None

        soup = None
        for parser in ("html5lib", "lxml", "html.parser"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, parser)
                break
            except Exception:
                continue

        if not soup:
            # fallback: regex extract <p> text
            raw_ps = re.findall(r"<p[^>]*>(.*?)</p>", html, re.S | re.I)
            candidates = []
            for raw in raw_ps:
                text = re.sub(r"<[^>]+>", "", raw).strip()
                text = re.sub(r"\s+", " ", text)
                if len(text) > 50 and not any(b in text for b in _BOILERPLATE):
                    candidates.append(text)
            return " ".join(candidates[:3]) if candidates else None

        candidates = []
        for p in soup.find_all("p"):
            text = p.get_text(separator=" ").strip()
            text = re.sub(r"\s+", " ", text)
            if len(text) > 50 and not any(b in text for b in _BOILERPLATE):
                candidates.append(text)

        return " ".join(candidates[:3]) if candidates else None

    # ------------------------------------------------------------------
    # Date parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_date(date_str):
        if not date_str:
            return None
        try:
            dt = datetime.strptime(date_str.strip(), "%m/%d/%Y %I:%M:%S %p")
            return dt.strftime("%Y-%m-%d")
        except Exception:
            pass
        m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
        if m:
            mo, d, y = m.groups()
            return f"{y}-{int(mo):02d}-{int(d):02d}"
        return date_str

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        """Crawl PHF Science corporate publications.

        Walks Algolia pages until ``limit`` records are saved, no more
        results exist, or the 200-page safety cap is reached.
        """
        saved = 0
        seen_urls = set()
        limit_or_inf = limit if limit is not None else float("inf")
        start_time = time.time()
        MAX_SECONDS = 25 * 60
        HITS_PER_PAGE = 50

        page = 0
        total_pages = None

        while True:
            # ------ safety checks ------
            if time.time() - start_time > MAX_SECONDS:
                print(
                    f"[{self.site_id}] Approaching 25-minute time limit — "
                    "stopping cleanly."
                )
                break

            if saved >= limit_or_inf:
                break

            if page >= 200:
                print(f"[{self.site_id}] Reached 200-page safety cap.")
                break

            if page % 10 == 0 and page > 0:
                lbl = limit if limit is not None else "∞"
                print(
                    f"[{self.site_id}] page {page}: "
                    f"saved {saved}/{lbl}"
                )

            # ------ fetch Algolia page ------
            try:
                data = self._algolia_query(page=page, hits_per_page=HITS_PER_PAGE)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(
                    f"[{self.site_id}] Algolia query failed on page "
                    f"{page}: {exc}"
                )
                break

            if not data:
                print(
                    f"[{self.site_id}] No data returned on page {page}, "
                    "stopping."
                )
                break

            hits = data.get("hits", [])
            if not hits:
                print(
                    f"[{self.site_id}] No more hits on page {page}, stopping."
                )
                break

            if total_pages is None:
                total_pages = data.get("nbPages", 1)
                nb_hits = data.get("nbHits", 0)
                print(
                    f"[{self.site_id}] Total records: {nb_hits}, "
                    f"pages: {total_pages}"
                )

            new_on_page = 0

            for hit in hits:
                if saved >= limit_or_inf:
                    break

                item_path = hit.get("url", "") or ""
                if item_path in seen_urls:
                    continue
                seen_urls.add(item_path)
                new_on_page += 1

                try:
                    title = (hit.get("title") or "").strip()
                    if not title:
                        continue

                    abstract = (hit.get("description") or "").strip()

                    # Enrich short abstracts via detail page
                    if len(abstract) < 100 and item_path:
                        page_text = self._extract_page_abstract(item_path)
                        time.sleep(self._delay)
                        if page_text and len(page_text) > len(abstract):
                            abstract = page_text.strip()

                    if len(abstract) < 50:
                        print(
                            f"[{self.site_id}] Skipping '{title[:60]}' — "
                            f"abstract too short ({len(abstract)} chars)"
                        )
                        continue

                    if len(abstract) < 100:
                        print(
                            f"[{self.site_id}] Skipping '{title[:60]}' — "
                            f"abstract still <100 chars ({len(abstract)})"
                        )
                        continue

                    object_id = hit.get("objectID") or str(hit.get("id", ""))
                    published_date = self._parse_date(hit.get("date", ""))

                    full_url = (
                        self.base_url + item_path
                        if item_path.startswith("/")
                        else item_path
                    )

                    authors_raw = (hit.get("authors") or "").strip()
                    if authors_raw:
                        authors = json.dumps(
                            [a.strip() for a in re.split(r",\s*", authors_raw)
                             if a.strip()]
                        )
                    else:
                        authors = json.dumps([])

                    metadata = {
                        "contentTypeAlias": hit.get("contentTypeAlias"),
                        "year": hit.get("year"),
                        "source": hit.get("source"),
                        "algoliaObjectId": object_id,
                        "imageUrl": hit.get("image"),
                    }

                    paper = {
                        "id": object_id,
                        "site_id": self.site_id,
                        "external_id": object_id,
                        "title": title,
                        "authors": authors,
                        "abstract": abstract,
                        "category": "corporatePublicationItem",
                        "keywords": json.dumps([]),
                        "published_date": published_date,
                        "url": full_url,
                        "pdf_url": None,
                        "doi": None,
                        "department": hit.get("department"),
                        "metadata": json.dumps(metadata),
                    }

                    self._save_paper(paper)
                    saved += 1

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(
                        f"[{self.site_id}] item {item_path!r} failed: {exc}"
                    )
                    continue

            # ------ pagination checks ------
            if page + 1 >= (total_pages or 1):
                print(
                    f"[{self.site_id}] Reached last Algolia page ({page}), "
                    "stopping."
                )
                break

            if new_on_page == 0:
                print(
                    f"[{self.site_id}] No new records on page {page}, "
                    "stopping (dedup loop guard)."
                )
                break

            page += 1

        lbl = limit if limit is not None else "∞"
        print(f"[{self.site_id}] Crawl complete: saved {saved}/{lbl} records.")
        return saved
