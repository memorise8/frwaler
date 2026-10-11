"""ADR UK's two original pagers and annual-report alias reconciliation."""
import copy
from contextlib import contextmanager
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from .document_dates import normalize_date
from .incremental import Entry, Page, Store, UnsupportedSource
from .incremental_adapters import Adapter


def day(item):
    value = normalize_date(item['listed_date_raw'])
    if value['status'] != 'normalized' or value['precision'] != 'day':
        raise UnsupportedSource('adruk_precise_card_date_unavailable')
    return value['value']


class AdrukAdapter(Adapter):
    rows = 10

    def __init__(self, crawler, *, scopes=None):
        self.native = crawler.crawl.__globals__
        selected = list(scopes) if scopes is not None else list(self.native['_SCOPES'])
        if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(self.native['_SCOPES']):
            raise ValueError('unknown ADR UK delivered scope')
        super().__init__(crawler, {'scopes': selected, 'rows': self.rows})
        self.contract = hashlib.sha256(self.contract.encode() + Path(__file__).read_bytes()
            + Path(crawler.crawl.__code__.co_filename).read_bytes()
            + Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()
        # Collector resets cursor for a new generation. Retain only completed,
        # contract-matching alias proofs; pending delivery never certifies them.
        self.previous = Store(crawler._conn, crawler.site_id).state()

    def html(self, url, *, detail=False):
        self.http_requests += 1
        if detail:
            self.detail_requests += 1
        response = self.crawler._request(url, retries=1)
        if response is None:
            raise ConnectionError('ADR UK source HTML unavailable')
        return response.text

    @contextmanager
    def native_transport(self):
        c = self.crawler
        old_html, old_request = c._html, c._session.request
        def request(method, url, **kwargs):
            if urlsplit(url).path.startswith('/fileadmin/'):
                self.http_requests += 1
            return old_request(method, url, **kwargs)
        c._session.request = request
        c._html = lambda url: self.html(url, detail=('/article/' in url or '/publication/' in url))
        try:
            yield
        finally:
            c._html, c._session.request = old_html, old_request

    def read(self, scope, page):
        data = self.native['_listing'](self.html(self.native['_list_url'](scope, page)), scope, page)
        dates = [day(x) for x in data['items']]
        if dates != sorted(dates, reverse=True):
            raise UnsupportedSource('adruk_card_date_order_changed')
        if len({x['slug'] for x in data['items']}) != len(data['items']):
            raise UnsupportedSource('adruk_ambiguous_source_slugs')
        return data

    def annual_candidate(self, item, annual):
        year = re.search(r'20\d{2}', item['list_title']) if 'annual report' in item['list_title'].lower() else None
        candidates = {u: r for u, r in annual.items() if year and r['year'] == year.group()}
        if len(candidates) > 1:
            raise ValueError('ambiguous annual report identity')
        return candidates

    def fetch(self, cursor, direction):
        incremental = direction == 'newest'
        if cursor is None:
            with self.native_transport():
                annual = self.crawler._annual_sources() if 'publications' in self.scope['scopes'] else {}
            prior = self.previous
            certified = ((prior.get('cursor') or {}).get('annual_certified', {})
                         if incremental and prior.get('contract') == self.contract
                         and prior.get('phase') == 'idle' and prior.get('baseline_complete')
                         and prior.get('support') == 'supported' else {})
            cursor = {'annual': annual,
                      'annual_certified': {u: r for u, r in annual.items() if certified.get(u) == r},
                      'identities': {}, 'streams': [dict(scope=s, page=0, buffer=[], done=False,
                          total=None, last=0, anchor=None, seen=[], last_date=None, known_pages=0,
                          unknown_ids=[]) for s in self.scope['scopes']]}
        else:
            cursor = copy.deepcopy(cursor)
        if [s['scope'] for s in cursor['streams']] != self.scope['scopes']:
            raise ValueError('ADR UK cursor scope mismatch')
        entries = []
        while len(entries) < self.rows:
            for stream in cursor['streams']:
                if stream['done'] or (stream['buffer'] and (not incremental or stream['page'] >= 2)):
                    continue
                scope, page = stream['scope'], stream['page']
                if stream['anchor'] is not None:
                    previous = self.read(scope, page - 1)
                    if (previous['page_hash'], previous['last'], previous['total']) != (stream['anchor'], stream['last'], stream['total']):
                        raise ValueError('ADR UK previous page changed')
                data = self.read(scope, page)
                items = data['items']; ids = [x['slug'] for x in items]
                if stream['total'] is not None and data['total'] != stream['total']:
                    raise ValueError('ADR UK source total changed')
                if items and data['first'] != stream['last'] + 1:
                    raise ValueError('ADR UK positions repeated or skipped')
                if set(ids).intersection(stream['seen']):
                    raise ValueError('ADR UK repeated source identity')
                if items and stream['last_date'] is not None and day(items[0]) > stream['last_date']:
                    raise UnsupportedSource('adruk_cross_page_date_order_changed')
                unknown = []
                for item in items:
                    key = item['slug']
                    if key in cursor['identities'] and cursor['identities'][key] != item['url']:
                        raise UnsupportedSource('adruk_cross_scope_slug_collision')
                    cursor['identities'][key] = item['url']
                    self.crawler._assert_stored_identity(item)
                    if incremental and (not Store(self.crawler._conn, self.crawler.site_id).known(key) or not self.exists(Entry(key, item))):
                        unknown.append(key)
                stream['seen'].extend(ids); stream['unknown_ids'].extend(unknown)
                stream.update(buffer=stream['buffer'] + [dict(x, _source_scope=scope) for x in items],
                              page=page + 1, done=data['last'] == data['total'], total=data['total'],
                              last=data['last'], anchor=data['page_hash'],
                              last_date=day(items[-1]) if items else stream['last_date'],
                              known_pages=stream['known_pages'] + 1 if incremental and items and not unknown else 0)
            candidates = [s for s in cursor['streams'] if s['buffer']]
            if not candidates:
                break
            stream = max(candidates, key=lambda s: (day(s['buffer'][0]), s['buffer'][0]['slug']))
            item = stream['buffer'].pop(0); key = item['slug']
            item['_annual'] = self.annual_candidate(item, cursor['annual'])
            # Verify root->publication->PDF identity before certifying any alias.
            # Cache the paper in the durable payload; delivery remains oldest first.
            if any(cursor['annual_certified'].get(u) != r for u, r in item['_annual'].items()):
                item['_prepared_paper'] = self.materialize(item)
                cursor['annual_certified'].update(item['_annual'])
            if key in stream['unknown_ids']:
                stream['unknown_ids'].remove(key)
            entries.append(Entry(key, item, day(item)))
        exhausted = all(s['done'] and not s['buffer'] for s in cursor['streams'])
        annual_complete = cursor['annual_certified'] == cursor['annual']
        if exhausted and not annual_complete:
            raise ValueError('annual report index is not fully represented in publications')
        safe = annual_complete and all((s['done'] or s['known_pages'] >= 2) and not s['unknown_ids'] for s in cursor['streams'])
        return Page(entries, cursor, exhausted, 'selected original ranges and annual aliases exhausted' if exhausted else '',
                    boundary_safe=safe if incremental else True)

    def materialize(self, item):
        self.crawler._assert_stored_identity(item)
        if '_prepared_paper' in item:
            return item['_prepared_paper']
        with self.native_transport():
            paper, matched, _ = self.crawler._materialize(item, item['_source_scope'], item['_annual'])
        if item['_annual'] and matched not in item['_annual']:
            raise ValueError('annual representation mismatch')
        return paper
