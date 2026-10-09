"""Bounded HAL traversal with validated source anchors and item checkpoints.

Only reviewed adapters use this helper. An offset is not an immutable source
snapshot: changed totals or anchor records stop the run for reconciliation.
"""
import hashlib
import json
from pathlib import Path
import time

VERSION = 'hal-offset-anchor-v1'


def crawl_hal(crawler, fetch, make_paper, *, limit, rows, max_pages, wall_seconds):
    if any(type(v) is not int or v <= 0 for v in (rows, max_pages)) or wall_seconds <= 0:
        crawler._report_source_failure('configuration')
        return 0
    started = time.monotonic()
    digest = hashlib.sha256(Path(type(crawler).crawl.__code__.co_filename).read_bytes())
    digest.update(Path(__file__).read_bytes())
    source_hash = digest.hexdigest()
    saved = 0
    offset = 0
    total = None
    seen = set()

    def expired():
        crawler._check_cancelled()
        return time.monotonic() - started >= wall_seconds

    def read(start):
        try:
            data = fetch(start, rows)
        except Exception:
            crawler._report_source_failure('fetch_failed')
            return None
        response = data.get('response') if isinstance(data, dict) else None
        docs = response.get('docs') if isinstance(response, dict) else None
        found = response.get('numFound') if isinstance(response, dict) else None
        if (not isinstance(docs, list) or type(found) is not int or found < 0
                or len(docs) > rows or start + len(docs) > found
                or any(not isinstance(d, dict) or not str(d.get('docid') or '') for d in docs)):
            crawler._report_source_failure('invalid_response')
            return None
        ids = [str(d['docid']) for d in docs]
        if len(ids) != len(set(ids)):
            crawler._report_source_failure('invalid_response')
            return None
        return docs, found, ids, hashlib.sha256(json.dumps(docs, sort_keys=True).encode()).hexdigest()

    cursor = crawler.delivery_cursor
    if cursor:
        keys = ('offset', 'anchor_start', 'anchor_count', 'rows', 'total')
        if (not isinstance(cursor, dict) or cursor.get('version') != VERSION
                or cursor.get('site_id') != crawler.site_id or cursor.get('source_hash') != source_hash
                or any(type(cursor.get(k)) is not int for k in keys)
                or cursor['rows'] != rows or cursor['anchor_start'] < 0
                or not 0 < cursor['anchor_count'] <= rows
                or not cursor['anchor_start'] < cursor['offset'] <= cursor['anchor_start'] + cursor['anchor_count'] <= cursor['total']):
            crawler._report_source_failure('configuration')
            return saved
        if expired(): return saved
        anchor = read(cursor['anchor_start'])
        if anchor is None: return saved
        docs, found, ids, fingerprint = anchor
        if (found != cursor['total'] or len(docs) != cursor['anchor_count']
                or fingerprint != cursor.get('anchor_hash')):
            crawler._report_source_failure('invalid_response')
            return saved
        offset, total = cursor['offset'], found
        seen.update(ids[:offset-cursor['anchor_start']])

    for _ in range(max_pages):
        if expired() or (limit is not None and saved >= limit): break
        page = read(offset)
        if page is None: break
        docs, found, ids, fingerprint = page
        if total is None: total = found
        if found != total or seen.intersection(ids):
            crawler._report_source_failure('invalid_response')
            break
        if not docs:
            if offset == total:
                crawler._mark_exhausted()
            else:
                crawler._report_source_failure('invalid_response')
            break
        page_start = offset
        for index, doc in enumerate(docs, 1):
            if expired() or (limit is not None and saved >= limit): return saved
            try:
                paper = make_paper(doc)
                if paper is not None:
                    crawler._save_paper(paper)
                    if crawler._last_save_created: saved += 1
            except Exception:
                crawler._report_source_failure('item_failed')
                return saved
            if crawler.delivery_source_failure: return saved
            # Each successful save or reviewed code exclusion commits progress.
            # Failed, unprocessed, and cancelled items never advance the offset.
            offset = page_start + index
            crawler._advance_cursor({'version': VERSION, 'site_id': crawler.site_id,
                                     'source_hash': source_hash, 'offset': offset,
                                     'anchor_start': page_start, 'anchor_count': len(docs),
                                     'anchor_hash': fingerprint, 'rows': rows, 'total': total}, items_done=1)
            seen.add(str(doc['docid']))
    return saved
