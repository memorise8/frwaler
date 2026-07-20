# -*- coding: utf-8 -*-
"""Nemertes (University of Patras institutional repository) crawler.

Data source: DSpace 7.6.1 REST API. The site's Angular UI has broken
server-side rendering on this deployment — *every* deep-linked route
(``/browse/title``, ``/items/<uuid>``, ``/handle/<handle>``, ``/search``)
404s under a plain HTTP client, including the exact starting URL given for
this crawler — but the underlying REST API behind that UI is fully
accessible and returns the same data the browse page would render.

  - List:  GET {base}/server/api/discover/browses/title/items
               ?scope=<collection uuid>&size=<n>&page=<p>
           -> ``_embedded.items[]`` carries the *full* item metadata block
           (``dc.*`` / ``datacite.*`` / ``oaire.*`` fields), including
           ``dc.description.abstract``, ``dc.date.issued``,
           ``dc.date.accessioned``, ``dc.contributor.author``,
           ``dc.subject`` etc. — so no separate detail-page fetch is
           needed for title/abstract/dates/authors/keywords. This is the
           REST equivalent of the ``/browse/title?scope=...`` UI page.

  - PDF:   GET {base}/server/api/core/items/<uuid>/bundles?embed=bitstreams
           -> the bundle named ``ORIGINAL`` holds the actual PDF
           bitstream; its ``_links.content.href`` is the direct download
           URL and its ``name`` is the original filename. One extra
           request per item (rate-limited).

  - Item page: ``https://hdl.handle.net/<handle>`` — the DSpace-issued
    permalink for the item. Recorded as ``url`` in preference to the
    Angular UI route (``{base}/items/<uuid>``) since the handle redirect
    (302 -> the repository) is the only item-facing URL on this site
    confirmed to actually resolve for a plain HTTP client.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


_SCOPE = "aa75d318-67b9-41ef-b89c-d33db195a08e"
_PAGE_SIZE = 20
_PAGE_CAP = 200
_CRAWL_BUDGET_SECONDS = 25 * 60
_RETRY_WAITS = (1, 3, 9)
_MIN_ABSTRACT_CHARS = 50
_DETAIL_DELAY = 1.0

_DATE_RE_FULL = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_DATE_RE_MONTH = re.compile(r"^(\d{4})-(\d{2})$")
_DATE_RE_YEAR = re.compile(r"^(\d{4})$")


def _clean(value):
    if value is None:
        return ""
    return " ".join(str(value).split())


def _first_value(meta, key):
    vals = meta.get(key)
    if isinstance(vals, list) and vals:
        v = vals[0].get("value") if isinstance(vals[0], dict) else vals[0]
        v = _clean(v)
        return v or None
    return None


def _all_values(meta, key):
    vals = meta.get(key)
    out = []
    if isinstance(vals, list):
        for v in vals:
            raw = v.get("value") if isinstance(v, dict) else v
            raw = _clean(raw)
            if raw:
                out.append(raw)
    return out


def _parse_date(raw):
    """Normalize DSpace date strings (``YYYY-MM-DDThh:mm:ssZ`` / ``YYYY-MM-DD``
    / ``YYYY-MM`` / ``YYYY``) to ``YYYY-MM-DD``. Returns ``None`` on failure."""
    if not raw:
        return None
    s = str(raw).strip()
    m = _DATE_RE_FULL.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _DATE_RE_MONTH.match(s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01"
    m = _DATE_RE_YEAR.match(s)
    if m:
        return f"{m.group(1)}-01-01"
    return None


class NemertesLibraryUpatrasGrBrowseCrawler(BaseCrawler):
    site_id = "nemertes-library-upatras-gr-browse"
    site_name = "Custom: nemertes-library-upatras-gr-browse"
    base_url = "https://nemertes.library.upatras.gr"

    # ------------------------------------------------------------------
    # low-level HTTP helper (own retry/backoff schedule, per spec)
    # ------------------------------------------------------------------

    def _fetch_json(self, url, params=None):
        last_exc = None
        for attempt in range(3):
            try:
                resp = self._session.get(url, params=params, timeout=30)
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError:
                    text = resp.content.decode(resp.encoding or "utf-8", errors="replace")
                    return json.loads(text)
            except Exception as exc:  # noqa: BLE001 - defensive, must never crash the run
                last_exc = exc
                print(f"[{self.site_id}] request error (attempt {attempt + 1}/3) for {url}: {exc}")
                if attempt < len(_RETRY_WAITS):
                    time.sleep(_RETRY_WAITS[attempt])
        print(f"[{self.site_id}] giving up on {url}: {last_exc}")
        return None

    def _extract_pdf(self, bundles):
        """Return (pdf_url, original_filename) from a bundles+bitstreams payload."""
        if not isinstance(bundles, dict):
            return None, None
        try:
            for bundle in bundles.get("_embedded", {}).get("bundles", []):
                if bundle.get("name") != "ORIGINAL":
                    continue
                bitstreams = (
                    bundle.get("_embedded", {})
                    .get("bitstreams", {})
                    .get("_embedded", {})
                    .get("bitstreams", [])
                )
                for bs in bitstreams:
                    href = bs.get("_links", {}).get("content", {}).get("href")
                    name = bs.get("name")
                    if href:
                        return href, name
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.site_id}] bundle parse error: {exc}")
        return None, None

    # ------------------------------------------------------------------
    # per-item processing
    # ------------------------------------------------------------------

    def _process_item(self, item, item_url):
        meta = item.get("metadata") or {}

        title = _first_value(meta, "dc.title") or _clean(item.get("name")) or None
        if not title:
            print(f"[{self.site_id}] skip {item.get('uuid')}: no title")
            return False

        abstract = (
            _first_value(meta, "dc.description.abstract")
            or _first_value(meta, "dc.description.translatedabstract")
            or ""
        )
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skip {item.get('uuid')}: abstract too short ({len(abstract)} chars)")
            return False

        uuid_ = item.get("uuid") or item.get("id")
        handle = item.get("handle")

        # DSpace handles are ``<prefix>/<local-id>`` where ``<local-id>`` is a
        # monotonically-assigned sequential integer — a far better basis for
        # incremental "MAX(post_number)" crawling than the opaque UUID.
        post_number = None
        if handle and "/" in handle:
            candidate = handle.rsplit("/", 1)[-1]
            if candidate.isdigit():
                post_number = candidate
        if not post_number:
            post_number = uuid_

        published_raw = _first_value(meta, "dc.date.issued")
        published_date = _parse_date(published_raw)
        listed_raw = _first_value(meta, "dc.date.accessioned")
        listed_date = _parse_date(listed_raw)

        # Pass raw lists (not pre-joined strings) for authors/publisher: DSpace
        # author values are "Last, First" and a pre-joined string would be
        # mis-split on the comma by the base adapter's heuristic re-joiner.
        authors = _all_values(meta, "dc.contributor.author") or None
        publisher = _all_values(meta, "dc.publisher") or None

        keywords_list = _all_values(meta, "dc.subject") + _all_values(meta, "dc.subject.alternative")
        # de-dupe, preserve order; pass as a list so the base adapter's own
        # comma-joiner handles it (avoids double comma-splitting artifacts).
        keywords = list(dict.fromkeys(keywords_list)) or None

        category = _first_value(meta, "dc.type") or None
        doi = _first_value(meta, "dc.identifier.doi")
        series = _first_value(meta, "dc.relation.ispartofseries")
        journal_raw = (
            _first_value(meta, "dc.relation.ispartof")
            or _first_value(meta, "oaire.citation.title")
            or _first_value(meta, "oaire.citationTitle")
            or series
            or _first_value(meta, "dc.source")
        )
        volume = _first_value(meta, "oaire.citation.volume")
        issue = _first_value(meta, "oaire.citation.issue")

        pdf_url = None
        original_filename = None
        try:
            time.sleep(_DETAIL_DELAY)
            bundles = self._fetch_json(
                f"{self.base_url}/server/api/core/items/{uuid_}/bundles",
                params={"embed": "bitstreams"},
            )
            pdf_url, original_filename = self._extract_pdf(bundles)
        except Exception as exc:  # noqa: BLE001
            print(f"[{self.site_id}] bundle fetch failed for {uuid_}: {exc}")

        metadata_dict = {
            "posted_date": listed_raw,
            "originalFilename": original_filename,
            "journal_raw": journal_raw,
            "series": series,
            "volume": volume,
            "issue": issue,
            "item_uuid": uuid_,
            "handle": handle,
            "dc_type": category,
            "language": _first_value(meta, "dc.language.iso"),
            "degree": _first_value(meta, "dc.degree"),
            "raw_metadata": meta,
        }

        paper = {
            "site_id": self.site_id,
            "external_id": uuid_,
            "post_number": post_number,
            "title": title,
            "abstract": abstract,
            "published_date": published_date,
            "posted_date": listed_date,
            "authors": authors,
            "publisher": publisher,
            "department": None,
            "journal": journal_raw,
            "url": item_url,
            "pdf_url": pdf_url,
            "keywords": keywords,
            "category": category,
            "doi": doi,
            "original_filename": original_filename,
            "metadata": json.dumps(metadata_dict, ensure_ascii=False),
        }

        self._save_paper(paper)
        return True

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        start_time = time.time()
        saved = 0
        page = 0
        seen_urls = set()
        limit_display = limit if limit is not None else "inf"

        try:
            while True:
                if limit is not None and saved >= limit:
                    break
                if page >= _PAGE_CAP:
                    print(f"[{self.site_id}] hit safety page cap ({_PAGE_CAP} pages), stopping")
                    break
                elapsed = time.time() - start_time
                if elapsed > _CRAWL_BUDGET_SECONDS:
                    print(f"[{self.site_id}] approaching time budget ({elapsed:.0f}s elapsed), stopping cleanly")
                    break
                if page % 10 == 0:
                    print(f"[{self.site_id}] page {page}: saved {saved}/{limit_display}")

                data = self._fetch_json(
                    f"{self.base_url}/server/api/discover/browses/title/items",
                    params={
                        "scope": _SCOPE,
                        "size": _PAGE_SIZE,
                        "page": page,
                    },
                )
                if not data:
                    print(f"[{self.site_id}] page {page}: fetch failed, stopping")
                    break

                try:
                    items_on_page = data["_embedded"]["items"]
                except (KeyError, TypeError):
                    items_on_page = []

                if not items_on_page:
                    print(f"[{self.site_id}] page {page}: no items returned, stopping")
                    break

                new_on_page = 0
                for item in items_on_page:
                    if limit is not None and saved >= limit:
                        break
                    try:
                        if not isinstance(item, dict):
                            continue
                        uuid_ = item.get("uuid") or item.get("id")
                        if not uuid_:
                            continue
                        handle = item.get("handle")
                        item_url = f"https://hdl.handle.net/{handle}" if handle else f"{self.base_url}/items/{uuid_}"
                        if item_url in seen_urls:
                            continue
                        seen_urls.add(item_url)

                        if self._process_item(item, item_url):
                            saved += 1
                            new_on_page += 1
                    except Exception as exc:  # noqa: BLE001 - one bad item must never abort the run
                        bad_id = item.get("uuid") if isinstance(item, dict) else "?"
                        print(f"[{self.site_id}] item {bad_id} failed: {exc}")
                        continue

                if new_on_page == 0:
                    print(f"[{self.site_id}] page {page}: 0 new records saved, stopping")
                    break

                total_pages = (data.get("page") or {}).get("totalPages")
                if total_pages is not None and page >= total_pages - 1:
                    print(f"[{self.site_id}] page {page}: reached last page ({total_pages} total), stopping")
                    break

                page += 1
        except KeyboardInterrupt:
            raise

        print(f"[{self.site_id}] done: saved {saved} papers across {page + 1} page(s)")
        return saved
