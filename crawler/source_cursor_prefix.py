"""Revalidate source listings and stored rows before resuming an ordered cursor."""
import hashlib
import json
import os
import time


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def prefix_seed(contract):
    return _hash(['source-prefix', contract])


def advance_prefix(prefix, scope, page, page_hash, bounds):
    return _hash([prefix, scope, page, page_hash, list(bounds)])


def _stored_item(crawler, item, identity_key, date_key):
    postgres = os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite').lower() == 'postgres'
    if not postgres and not crawler._conn_is_libertree():
        raise ValueError('backfill prefix requires the delivery document database')
    mark = '%s' if postgres else '?'
    rows = crawler._conn.execute(
        f'SELECT post_number,meta_url,title,listed_date_raw,pdf_url FROM documents '
        f'WHERE site_id={mark} AND (post_number={mark} OR meta_url={mark})',
        (crawler.site_id, item[identity_key], item['url'])).fetchall()
    if len(rows) != 1:
        raise ValueError('source cursor prefix has a missing or ambiguous stored document')
    row = rows[0]
    keys = ('post_number', 'meta_url', 'title', 'listed_date_raw', 'pdf_url')
    values = tuple(row[key] for key in keys) if hasattr(row, 'keys') else tuple(row)
    expected = (item[identity_key], item['url'], item['title'], item[date_key], item.get('pdf_url'))
    # ACPR listing cards do not expose their detail page's primary PDF URL.
    if 'pdf_url' not in item:
        values, expected = values[:4], expected[:4]
    if tuple(value or '' for value in values) != tuple(value or '' for value in expected):
        raise ValueError('source cursor prefix differs from persisted source fields')


def verify_source_prefix(crawler, cursor, scopes, *, first_page, load_page,
                         bounds, identity_key, date_key, deadline):
    """Return the complete prefix hash and IDs without fetching prior details.

    Older version-1 cursors have no prefix hash. They still require every current
    prefix row and source field to match before acquiring a hash at the next
    checkpoint. New cursors also detect equal-count changes to earlier pages.
    """
    prefix, seen = prefix_seed(cursor['contract']), {}
    for scope in scopes:
        page, previous_hash, expected_bounds = first_page, '', None
        while True:
            crawler._check_cancelled()
            if time.monotonic() > deadline:
                raise ValueError('source prefix verification budget exceeded')
            data = load_page(scope, page)
            current_bounds = bounds(data)
            if expected_bounds is not None and current_bounds != expected_bounds:
                raise ValueError('source prefix pagination changed')
            expected_bounds = current_bounds
            at_cursor = scope == cursor['scope'] and page == cursor['page']
            if at_cursor:
                if (data['page_hash'] != cursor['page_hash']
                        or previous_hash != cursor['previous_hash']
                        or current_bounds != bounds(cursor)
                        or cursor['offset'] > len(data['items'])
                        or ('page_items' in cursor and cursor['page_items'] != len(data['items']))
                        or ('prefix_hash' in cursor and prefix != cursor['prefix_hash'])):
                    raise ValueError('source prefix or current cursor page changed')
            for index, item in enumerate(data['items']):
                if at_cursor and index >= cursor['offset']:
                    break
                crawler._check_cancelled()
                if time.monotonic() > deadline:
                    raise ValueError('source prefix verification budget exceeded')
                uid = item[identity_key]
                if uid in seen:
                    raise ValueError('source prefix repeats an earlier document')
                _stored_item(crawler, item, identity_key, date_key)
                seen[uid] = item['url']
            if at_cursor:
                return prefix, seen
            prefix = advance_prefix(prefix, scope, page, data['page_hash'], current_bounds)
            if not data['items'] or page >= data['last_page']:
                break
            previous_hash, page = data['page_hash'], page + 1
    raise ValueError('source prefix no longer reaches the saved cursor')
