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
    DELIVERY_CURSOR_VERSION = 1

    # ------------------------------------------------------------------
    # low-level HTTP helper (own retry/backoff schedule, per spec)
    # ------------------------------------------------------------------

    def _fetch_json(self, url, params=None):
        last_exc = None
        for attempt in range(3):
            self._check_cancelled()
            try:
                resp = self._session.get(url, params=params, timeout=30)
                self._check_cancelled()
                resp.raise_for_status()
                try:
                    return resp.json()
                except ValueError:
                    text = resp.content.decode(resp.encoding or "utf-8", errors="replace")
                    return json.loads(text)
            except Exception as exc:  # noqa: BLE001 - defensive, must never crash the run
                last_exc = exc
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                if status in (401, 403, 429):
                    self._report_source_failure('rate_limited' if status == 429 else 'authentication_failed')
                    return None
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
        self._item_consumed = False
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
        self._item_consumed = True
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

    @staticmethod
    def _page_identity(items):
        return hashlib.sha256(json.dumps([item.get('uuid') or item.get('id') for item in items],
                                         separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _cursor_contract():
        return hashlib.sha256(json.dumps([list(_SCOPES), _PAGE_SIZE], separators=(',', ':')).encode()).hexdigest()

    def _validate_cursor(self, cursor):
        if not isinstance(cursor, dict) or cursor.get('version') != self.DELIVERY_CURSOR_VERSION:
            raise ValueError('unsupported cursor')
        if cursor.get('contract') != self._cursor_contract() or cursor.get('scope') not in _SCOPES:
            raise ValueError('changed scope contract')
        for key in ('page', 'offset', 'consumed', 'total_pages', 'total'):
            if type(cursor.get(key)) is not int or cursor[key] < 0:
                raise ValueError('invalid position')
        if cursor['offset'] > _PAGE_SIZE:
            raise ValueError('invalid item offset')
        if cursor['consumed'] != cursor['page'] * _PAGE_SIZE:
            raise ValueError('invalid completed-page membership count')
        if cursor['total_pages'] != (cursor['total'] + _PAGE_SIZE - 1) // _PAGE_SIZE:
            raise ValueError('invalid saved source totals')
        for key, required in [('page_hash', cursor['offset'] > 0), ('previous_hash', cursor['page'] > 0)]:
            value = cursor.get(key)
            if not isinstance(value, str) or (value and not re.fullmatch('[0-9a-f]{64}', value)) or (required and not value):
                raise ValueError('invalid source anchor')

    def _checkpoint(self, scope, page, offset, consumed, total_pages, total, page_hash, previous_hash, items_done=0):
        if self._cursor_enabled:
            self._advance_cursor(dict(version=self.DELIVERY_CURSOR_VERSION, contract=self._cursor_contract(),
                                      scope=scope, page=page, offset=offset, consumed=consumed,
                                      total_pages=total_pages, total=total, page_hash=page_hash,
                                      previous_hash=previous_hash), items_done=items_done)

    def crawl(self, limit=None):
        self._check_cancelled()
        if limit is not None and limit <= 0:
            return 0
        self._cursor_enabled = self.delivery_mode == 'backfill'
        resume = self.delivery_cursor if self._cursor_enabled else None
        if resume:
            try:
                self._validate_cursor(resume)
            except (TypeError, ValueError):
                self._report_source_failure('configuration')
                return 0
        start_time = time.time()
        saved = 0
        seen_items = set()
        start_index = _SCOPES.index(resume['scope']) if resume else 0
        finished = False
        for scope in _SCOPES[start_index:]:
            self._check_cancelled()
            remaining = None if limit is None else limit - saved
            if remaining is not None and remaining <= 0:
                break
            count, finished = self._crawl_scope(scope, remaining, start_time, seen_items,
                                                resume if resume and resume['scope'] == scope else None)
            saved += count
            if not finished:
                break
        else:
            if finished and not self.delivery_source_failure:
                self._mark_exhausted()
        self._check_cancelled()
        print(f'[{self.site_id}] done: saved {saved} papers')
        return saved

    def _crawl_scope(self, scope, limit, start_time, seen_items, resume=None):
        """Resume only verified item positions; a failed item stops backfill.

        Title-ordered source pages are mutable. Re-check the previous page and
        current page identities/totals before using a saved offset. A detected
        change requires source review/re-enumeration, never a guessed skip.
        This is not an immutable snapshot or a substitute for full-source audits.
        """
        saved = 0
        page = resume['page'] if resume else 0
        offset = resume['offset'] if resume else 0
        consumed = resume['consumed'] if resume else 0
        expected_pages = resume['total_pages'] if resume else None
        expected_total = resume['total'] if resume else None
        previous_hash = resume['previous_hash'] if resume else ''
        previous_ids = set()
        scope_items = set()
        if resume and page > 0:
            prior = self._fetch_json(f'{self.base_url}/server/api/discover/browses/title/items',
                                     params={'scope': scope, 'size': _PAGE_SIZE, 'page': page - 1})
            if not prior:
                self._report_source_failure('fetch_failed')
                return saved, False
            try:
                info = prior['page'];items = prior['_embedded']['items']
                if (info['number'] != page - 1 or info['totalPages'] != expected_pages
                        or info['totalElements'] != expected_total or not isinstance(items, list)
                        or self._page_identity(items) != previous_hash):
                    raise ValueError('previous source boundary changed')
                previous_ids = {item.get('uuid') or item.get('id') for item in items}
            except (KeyError, TypeError, ValueError, AttributeError):
                self._report_source_failure('invalid_response')
                return saved, False
        while True:
            self._check_cancelled()
            if limit is not None and saved >= limit:
                return saved, False
            if expected_pages is None and page >= _PAGE_CAP:
                self._report_source_failure('invalid_response')
                return saved, False
            if time.time() - start_time > _CRAWL_BUDGET_SECONDS:
                if not self._cursor_enabled or self.delivery_pending_cursor is None:
                    self._report_source_failure('invalid_response')
                return saved, False
            data = self._fetch_json(f'{self.base_url}/server/api/discover/browses/title/items',
                                    params={'scope': scope, 'size': _PAGE_SIZE, 'page': page})
            self._check_cancelled()
            if not data:
                self._report_source_failure('fetch_failed')
                return saved, False
            try:
                if not isinstance(data, dict) or not isinstance(data.get('page', {}), dict):
                    raise ValueError('invalid source envelope')
                info = data.get('page', {})
                total_pages, total = info.get('totalPages'), info.get('totalElements')
                if (total_pages is not None and (type(total_pages) is not int or total_pages < 0)) or (
                        total is not None and (type(total) is not int or total < 0)):
                    raise ValueError('invalid source totals')
                if ('number' in info and (type(info['number']) is not int or info['number'] != page)) or (
                        expected_pages is not None and total_pages != expected_pages) or (
                        expected_total is not None and total != expected_total):
                    raise ValueError('source page or total changed')
                if self._cursor_enabled and (total is None or total_pages is None or info.get('number') != page):
                    raise ValueError('backfill requires explicit source page and total')
                embedded = data.get('_embedded', {})
                if not isinstance(embedded, dict):
                    raise ValueError('invalid source items')
                items = embedded.get('items', [] if total == 0 else None)
                if not isinstance(items, list):
                    raise ValueError('invalid source items')
                ids = [(item.get('uuid') or item.get('id')) if isinstance(item, dict) else None for item in items]
                if any(not isinstance(uid, str) or not uid for uid in ids) or len(set(ids)) != len(ids):
                    raise ValueError('invalid or duplicate source identities')
                if set(ids).intersection(scope_items | previous_ids):
                    raise ValueError('repeated source page identities')
                page_hash = self._page_identity(items)
                if resume and resume['page_hash'] and page_hash != resume['page_hash']:
                    raise ValueError('current source page identities changed')
                if offset > len(items):
                    raise ValueError('source page shorter than saved offset')
                if not items:
                    if (total is not None and consumed != total) or (total_pages is not None and page < total_pages and total != 0):
                        raise ValueError('premature empty source page')
                    return saved, True
                if total_pages == 0 or (total_pages is not None and page >= total_pages):
                    raise ValueError('source page beyond declared end')
            except (KeyError, TypeError, ValueError, AttributeError):
                self._report_source_failure('invalid_response')
                return saved, False
            expected_pages, expected_total = total_pages, total
            self._checkpoint(scope, page, offset, consumed, total_pages, total, page_hash, previous_hash)
            for index, item in enumerate(items):
                if index < offset:
                    continue
                self._check_cancelled()
                if limit is not None and saved >= limit:
                    return saved, False
                if time.time() - start_time > _CRAWL_BUDGET_SECONDS:
                    if not self._cursor_enabled:
                        self._report_source_failure('invalid_response')
                    return saved, False
                uuid_ = ids[index]
                if uuid_ not in seen_items:
                    handle = item.get('handle')
                    item_url = f'https://hdl.handle.net/{handle}' if handle else f'{self.base_url}/items/{uuid_}'
                    self._item_consumed = False
                    try:
                        if self._process_item(item, item_url):
                            saved += 1
                    except Exception as exc:
                        self._report_source_failure('item_failed')
                        print(f'[{self.site_id}] item {uuid_} failed: {exc}')
                    if self._cursor_enabled and not self._item_consumed:
                        self._report_source_failure('item_failed')
                        return saved, False
                    if getattr(self, '_pdf_access_held', False):
                        return saved, False
                    seen_items.add(uuid_)
                self._checkpoint(scope, page, index + 1, consumed, total_pages, total,
                                 page_hash, previous_hash, items_done=1)
                self._check_cancelled()
            scope_items.update(ids)
            consumed += len(items)
            if total_pages is not None and page == total_pages - 1:
                if total is not None and consumed != total:
                    self._report_source_failure('invalid_response')
                    return saved, False
                return saved, True
            previous_hash = page_hash
            previous_ids = set(ids)
            page += 1
            offset = 0
            resume = None
            self._checkpoint(scope, page, 0, consumed, total_pages, total, '', previous_hash)
