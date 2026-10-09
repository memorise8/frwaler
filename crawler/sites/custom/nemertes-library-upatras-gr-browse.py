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
import hashlib
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))
from crawler.base_crawler import BaseCrawler


# All five browse URLs assigned to this collector in the delivery URL table.
# Keep the previously collected scope first; UUID identity spans scopes.
_SCOPES = (
    "aa75d318-67b9-41ef-b89c-d33db195a08e",
    "38e1d29e-3f53-4755-868e-a36b75554f81",
    "9e4177d9-b92b-4778-a84a-e27f482addb4",
    "f8d85de1-7de0-4348-bca3-10784f86ae71",
    "d533961b-ecdb-497c-b850-8f8dd0594939",
)
_PAGE_SIZE = 20
_PAGE_CAP = 200  # Fallback only when the source does not report a page count.
_CRAWL_BUDGET_SECONDS = int(os.environ.get("LIBERTREE_MAX_WALL_S", str(25 * 60)))
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
            self._check_cancelled()
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
                    if href and isinstance(name, str) and name.lower().endswith('.pdf'):
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
        published_date = published_raw
        listed_raw = _first_value(meta, "dc.date.accessioned")
        listed_date = listed_raw

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
            self._check_cancelled()
            time.sleep(_DETAIL_DELAY)
            bundles = self._fetch_json(
                f"{self.base_url}/server/api/core/items/{uuid_}/bundles",
                params={"embed": "bitstreams"},
            )
            if not isinstance(bundles, dict) or ('_embedded' not in bundles and (bundles.get('page') or {}).get('totalElements') != 0):
                self._report_source_failure('item_failed')
                return False
            pdf_url, original_filename = self._extract_pdf(bundles)
        except Exception as exc:  # noqa: BLE001
            self._report_source_failure('item_failed')
            print(f"[{self.site_id}] bundle fetch failed for {uuid_}: {exc}")
            return False

        pdf_provenance = {}
        if len(abstract) < _MIN_ABSTRACT_CHARS and pdf_url:
            abstract, pdf_provenance = self._abstract_from_pdf(pdf_url, bundles)
        if len(abstract) < _MIN_ABSTRACT_CHARS:
            print(f"[{self.site_id}] skip {uuid_}: no substantial source abstract or PDF text")
            return False

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
            **pdf_provenance,
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
        return self._last_save_created

    def _abstract_from_pdf(self, pdf_url, bundles):
        """Read a bounded excerpt from the item's ORIGINAL repository bitstream.

        This temporary metadata enrichment does not publish file-worker blobs
        or mark PDF/text files as downloaded. Image PDFs remain explicit gaps.
        """
        from delivery.files.worker import run_stage

        bitstream = None
        for bundle in bundles.get('_embedded', {}).get('bundles', []):
            if bundle.get('name') != 'ORIGINAL':
                continue
            for entry in bundle.get('_embedded', {}).get('bitstreams', {}).get('_embedded', {}).get('bitstreams', []):
                if entry.get('_links', {}).get('content', {}).get('href') == pdf_url:
                    bitstream = entry
                    break
        if not bitstream:
            raise RuntimeError('PDF is not an ORIGINAL item bitstream')
        max_bytes = 10 * 1024 * 1024
        target = pdf_url
        started = time.monotonic()
        for _ in range(4):
            self._check_cancelled()
            parsed = urlsplit(target)
            if (parsed.scheme != 'https' or parsed.netloc != 'nemertes.library.upatras.gr'
                    or not re.fullmatch(r'/server/api/core/bitstreams/[0-9a-fA-F-]{36}/content', parsed.path)
                    or parsed.query or parsed.fragment):
                raise RuntimeError('PDF outside reviewed repository bitstream scope')
            with self._session.get(target, stream=True, timeout=(10,30), allow_redirects=False) as response:
                if response.status_code in (401,403,429):
                    self._pdf_access_held = True
                    self._report_source_failure('rate_limited' if response.status_code == 429 else 'authentication_failed')
                    raise RuntimeError('repository PDF access unavailable')
                if response.status_code in (301,302,303,307,308):
                    if not response.headers.get('Location'):
                        raise RuntimeError('repository PDF redirect lacks destination')
                    target = urljoin(target,response.headers['Location'])
                    continue
                if response.status_code != 200:
                    raise RuntimeError('repository PDF fetch failed')
                if int(response.headers.get('Content-Length','0')) > max_bytes:
                    raise RuntimeError('repository PDF size budget exceeded')
                content = bytearray()
                for chunk in response.iter_content(65536):
                    self._check_cancelled()
                    content.extend(chunk)
                    if len(content)>max_bytes or time.monotonic()-started>60:
                        raise RuntimeError('repository PDF download budget exceeded')
            break
        else:
            raise RuntimeError('repository PDF redirect budget exceeded')
        if not content.startswith(b'%PDF-'):
            raise RuntimeError('repository response is not PDF')
        expected_size = bitstream.get('sizeBytes')
        if expected_size is not None and expected_size != len(content):
            raise RuntimeError('repository PDF size disagrees with item metadata')
        checksum = bitstream.get('checkSum') or {}
        if checksum.get('checkSumAlgorithm','').upper() == 'MD5':
            if hashlib.md5(content).hexdigest() != checksum.get('value','').lower():
                raise RuntimeError('repository PDF checksum disagrees with item metadata')
        self._check_cancelled()
        with tempfile.TemporaryDirectory(prefix='nemertes-pdf-') as directory:
            root = Path(directory);(root/'source.pdf').write_bytes(content)
            result = run_stage('text',root,timeout=45)
            self._check_cancelled()
            if not result.get('success'):
                raise RuntimeError('repository PDF requires OCR' if result.get('error')=='no_text_layer' else 'repository PDF text extraction failed')
            with (root/'output.txt').open(encoding='utf-8') as handle:
                excerpt = _clean(handle.read(12000))[:3000]
            with (root/'output.txt').open('rb') as handle:
                text_hash = hashlib.file_digest(handle,'sha256').hexdigest()
        return excerpt,dict(abstract_source='repository_pdf_text_excerpt',abstract_pdf_url=pdf_url,
            abstract_pdf_final_url=target,abstract_pdf_sha256=hashlib.sha256(content).hexdigest(),
            abstract_pdf_text_sha256=text_hash,abstract_pdf_text_characters=result.get('chars'))

    # ------------------------------------------------------------------
    # crawl
    # ------------------------------------------------------------------

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        start_time = time.time()
        saved = 0
        seen_items = set()
        for scope in _SCOPES:
            self._check_cancelled()
            remaining = None if limit is None else limit - saved
            if remaining is not None and remaining <= 0:
                break
            count, finished = self._crawl_scope(scope, remaining, start_time, seen_items)
            saved += count
            if not finished:
                break
        self._check_cancelled()
        print(f"[{self.site_id}] done: saved {saved} papers")
        return saved

    def _crawl_scope(self, scope, limit, start_time, seen_items):
        """Traverse one source scope; cross-scope overlap is not a repeated page."""
        saved = 0
        page = 0
        scope_items = set()
        expected_pages = None
        expected_total = None
        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit:
                return saved, False
            if expected_pages is None and page >= _PAGE_CAP:
                self._report_source_failure('invalid_response')
                return saved, False
            if time.time() - start_time > _CRAWL_BUDGET_SECONDS:
                self._report_source_failure('invalid_response')
                return saved, False
            data = self._fetch_json(
                f"{self.base_url}/server/api/discover/browses/title/items",
                params={"scope": scope, "size": _PAGE_SIZE, "page": page},
            )
            if not data:
                self._report_source_failure('fetch_failed')
                return saved, False
            if not isinstance(data, dict) or not isinstance(data.get('page', {}), dict):
                self._report_source_failure('invalid_response')
                return saved, False
            info = data.get('page', {})
            total_pages = info.get('totalPages')
            total = info.get('totalElements')
            if (total_pages is not None and
                    (type(total_pages) is not int or total_pages < 0)) or (
                    total is not None and (type(total) is not int or total < 0)):
                self._report_source_failure('invalid_response')
                return saved, False
            if ('number' in info and (type(info['number']) is not int or info['number'] != page)) or (
                    expected_pages is not None and total_pages != expected_pages) or (
                    expected_total is not None and total != expected_total):
                self._report_source_failure('invalid_response')
                return saved, False
            expected_pages = total_pages
            expected_total = total
            embedded = data.get('_embedded', {})
            if not isinstance(embedded, dict):
                self._report_source_failure('invalid_response')
                return saved, False
            items = embedded.get('items', [] if total == 0 else None)
            if not isinstance(items, list):
                self._report_source_failure('invalid_response')
                return saved, False
            if not items:
                if (total is not None and len(scope_items) != total) or (
                        total_pages is not None and page < total_pages and total != 0):
                    self._report_source_failure('invalid_response')
                    return saved, False
                return saved, True
            if total_pages == 0 or (total_pages is not None and page >= total_pages):
                self._report_source_failure('invalid_response')
                return saved, False
            new_in_scope = 0
            for item in items:
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    return saved, False
                uuid_ = (item.get('uuid') or item.get('id')) if isinstance(item, dict) else None
                if not isinstance(uuid_, str) or not uuid_:
                    self._report_source_failure('invalid_response')
                    continue
                if uuid_ in scope_items:
                    self._report_source_failure('invalid_response')
                    continue
                scope_items.add(uuid_)
                new_in_scope += 1
                if uuid_ in seen_items:
                    continue
                seen_items.add(uuid_)
                handle = item.get('handle')
                item_url = f"https://hdl.handle.net/{handle}" if handle else f"{self.base_url}/items/{uuid_}"
                try:
                    if self._process_item(item, item_url):
                        saved += 1
                except Exception as exc:
                    self._report_source_failure('item_failed')
                    print(f"[{self.site_id}] item {uuid_} failed: {exc}")
                if getattr(self, '_pdf_access_held', False):
                    return saved, False
            if new_in_scope == 0:
                self._report_source_failure('invalid_response')
                return saved, False
            if total_pages is not None and page == total_pages - 1:
                if total is not None and len(scope_items) != total:
                    self._report_source_failure('invalid_response')
                    return saved, False
                return saved, True
            page += 1
