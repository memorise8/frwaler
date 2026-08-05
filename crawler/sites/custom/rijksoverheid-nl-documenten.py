# -*- coding: utf-8 -*-
"""Rijksoverheid.nl – Ambtsberichten crawler.

As of 2026-04-28 rijksoverheid.nl retired its own `/documenten` listing for
most document types (Kamerstukken, Woo-verzoeken, Ambtsberichten, ...) and
now redirects users to the KOOP "Open overheid" platform
(https://open.overheid.nl). The old `/documenten?type=Ambtsbericht` listing
page now renders an empty results list — the data has moved, not
disappeared.

Open overheid is a client-side React app; the actual documents come from a
plain, unauthenticated JSON REST API (no bot-defense observed):

    GET https://open.overheid.nl/overheid/openbaarmakingen/api/v0/zoek
        ?start=<offset>&aantalResultaten=<pageSize>&documentsoort=ambtsbericht

`documentsoort=ambtsbericht` matches the "documentsoort" facet value
confirmed via the API's own facet counts (974 documents at time of writing).
Each result already carries title/description/date/publisher; the `pid`
field is a direct, permanent PDF download URL (there is no separate HTML
detail page — the SPA route "/documenten/<id>" itself streams the PDF).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from urllib.parse import urlencode

from crawler.base_crawler import BaseCrawler


class RijksoverheidNlDocumentenCrawler(BaseCrawler):
    site_id = "rijksoverheid-nl-documenten"
    site_name = "Custom: rijksoverheid-nl-documenten"
    base_url = "https://open.overheid.nl"

    _API_URL = "https://open.overheid.nl/overheid/openbaarmakingen/api/v0/zoek"
    _DOC_URL_TMPL = "https://open.overheid.nl/documenten/{doc_id}"
    _DOCUMENTSOORT = "ambtsbericht"
    _PAGE_SIZE = 50
    _MIN_ABSTRACT = 50
    _MAX_PAGES = int(os.environ.get("LIBERTREE_MAX_PAGES", "200"))
    _BACKOFF = (1, 3, 9)

    # ------------------------------------------------------------------
    # Network
    # ------------------------------------------------------------------

    def _fetch_page(self, start: int) -> dict | None:
        params = {
            "start": start,
            "aantalResultaten": self._PAGE_SIZE,
            "documentsoort": self._DOCUMENTSOORT,
        }
        url = f"{self._API_URL}?{urlencode(params)}"
        cmd = [
            "curl", "-sk", "--tls-max", "1.3", "--max-time", "30",
            "-A", self.USER_AGENT,
            "-H", "Accept: application/json",
            url,
        ]
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=35)
                raw = result.stdout
                if not raw:
                    raise RuntimeError("empty response")
                return json.loads(raw.decode("utf-8", errors="replace"))
            except Exception as exc:
                last_error = str(exc)
                print(f"[{self.site_id}] fetch attempt {attempt}/3 failed (start={start}): {last_error}")
                if attempt < 3:
                    time.sleep(self._BACKOFF[attempt - 1])
        print(f"[{self.site_id}] fetch failed after 3 attempts (start={start}): {last_error}")
        return None

    # ------------------------------------------------------------------
    # Main crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        saved = 0
        seen_ids: set[str] = set()
        start_time = time.time()
        max_seconds = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
        limit_str = str(limit) if limit is not None else "∞"

        for page in range(self._MAX_PAGES):
            if limit is not None and saved >= limit:
                break
            if time.time() - start_time > max_seconds:
                print(f"[{self.site_id}] 25-minute budget reached; stopping cleanly")
                break

            if page % 10 == 0:
                print(f"[{self.site_id}] page {page}: saved {saved}/{limit_str}")

            data = self._fetch_page(page * self._PAGE_SIZE)
            if data is None:
                print(f"[{self.site_id}] page {page}: fetch failed; stopping")
                break

            results = data.get("resultaten") or []
            if not results:
                print(f"[{self.site_id}] page {page}: no results; done")
                break

            new_on_page = 0
            for item in results:
                if limit is not None and saved >= limit:
                    break

                doc = item.get("document") or {}
                doc_id = doc.get("id")
                if not doc_id or doc_id in seen_ids:
                    continue
                seen_ids.add(doc_id)
                new_on_page += 1

                try:
                    title = (doc.get("titel") or "").strip()
                    abstract = (doc.get("omschrijving") or "").strip()
                    if not title or len(abstract) < self._MIN_ABSTRACT:
                        print(
                            f"[{self.site_id}] skip {doc_id}: abstract too short "
                            f"({len(abstract)} chars): {title[:60]}"
                        )
                        continue

                    doc_url = self._DOC_URL_TMPL.format(doc_id=doc_id)
                    published_date = doc.get("openbaarmakingsdatum") or None
                    listed_date = (doc.get("mutatiedatumtijd") or "")[:10] or published_date

                    paper = {
                        "site_id": self.site_id,
                        "external_id": doc_id,
                        "post_number": doc_id,
                        "title": title,
                        "abstract": abstract,
                        "published_date": published_date,
                        "listed_date": listed_date,
                        "posted_date": listed_date,
                        "authors": "",
                        "publisher": doc.get("publisher") or "",
                        "journal": "",
                        "url": doc_url,
                        "pdf_url": doc_url,
                        "keywords": "",
                        "category": self._DOCUMENTSOORT,
                        "doi": "",
                        "original_filename": None,
                        "metadata": json.dumps({
                            "posted_date": listed_date,
                            "aanbieder": doc.get("aanbieder"),
                            "mutatiedatumtijd": doc.get("mutatiedatumtijd"),
                            "bestandsgrootte": item.get("bestandsgrootte"),
                            "aantalPaginas": item.get("aantalPaginas"),
                            "bestandsType": item.get("bestandsType"),
                            "documentsoort": self._DOCUMENTSOORT,
                        }, ensure_ascii=False),
                    }

                    self._save_paper(paper)
                    saved += 1
                    print(f"[{self.site_id}] Saved {saved}/{limit_str}: {title[:60]}")

                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"[{self.site_id}] item {doc_id} failed: {exc}")
                    continue

                time.sleep(self._delay)

            if new_on_page == 0:
                print(f"[{self.site_id}] page {page}: no new records; stopping")
                break

            total = data.get("totaal")
            if total is not None and (page + 1) * self._PAGE_SIZE >= total:
                print(f"[{self.site_id}] reached reported total ({total}); done")
                break

        print(f"[{self.site_id}] Done. Total saved: {saved}")
        return saved
