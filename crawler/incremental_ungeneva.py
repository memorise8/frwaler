"""UN Geneva press: dated URL identity, native metadata and exclusion proof."""
import copy
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from .incremental import Entry, Page, Store, UnsupportedSource, _date
from .incremental_adapters import Adapter


class UNGenevaAdapter(Adapter):
    rows = 10

    def __init__(self, crawler):
        super().__init__(crawler, {'endpoint': crawler._LIST_URL, 'rows': self.rows,
                                 'identity': 'full_dated_URL_preserving_native_reused_slug'})
        self.contract = hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('ungeneva_traversal.py').read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()
        state = Store(crawler._conn, crawler.site_id).state() if crawler._conn is not None else {}
        same = state.get('contract') == self.contract
        self.prior_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'idle' and state.get('baseline_complete') else {})
        self.resume_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'deliver' else {})

    def html(self, url, *, detail=False):
        self.http_requests += 1
        if detail:
            self.detail_requests += 1
        r = self.crawler._request(url, retries=1, headers={'Accept-Encoding': 'gzip, deflate'})
        if r is None:
            raise ConnectionError('UN Geneva source unavailable')
        return r.text

    def native(self, method, *args, detail=False):
        c = self.crawler
        original = c._ung_fetch
        c._ung_fetch = lambda url: self.html(url, detail=detail)
        try:
            return method(*args)
        finally:
            c._ung_fetch = original

    def number(self, href):
        p = urlsplit(urljoin(self.scope['endpoint'], href))
        base = urlsplit(self.scope['endpoint'])
        q = parse_qs(p.query, keep_blank_values=True)
        if ((p.scheme, p.netloc, p.path, p.fragment) != (base.scheme, base.netloc, base.path, '')
                or set(q) != {'page'} or len(q['page']) != 1 or not q['page'][0].isdecimal()):
            raise ValueError('UN Geneva pager scope changed')
        return int(q['page'][0])

    def read(self, page, *, empty_after=None):
        c = self.crawler
        url = self.scope['endpoint']+'?page='+str(page)
        raw = self.html(url)
        original = c._ung_fetch
        def captured(target):
            if target != url:
                raise ValueError('UN Geneva native list requested another scope')
            return raw
        c._ung_fetch = captured
        try:
            data = c._ung_page(page)
        finally:
            c._ung_fetch = original
        view = c._ung_soup(raw).select_one('.news-listing')
        selected = view.select('.pager a[aria-current="page"][href]')
        numbers = [self.number(a['href']) for a in view.select('.pager a[href]')]
        if len(selected) != 1 or not numbers:
            raise ValueError('UN Geneva current page proof missing')
        current = self.number(selected[0]['href'])
        if len(view.select('.views-row')) != len(data['items']):
            raise ValueError('UN Geneva skipped source cards')
        if not data['items']:
            if (empty_after is None or page != empty_after+1 or current != empty_after
                    or max(numbers) != empty_after or data['has_next']):
                raise ValueError('UN Geneva empty catalogue is not verified adjacent exhaustion')
        elif (current != page or not 1 <= len(data['items']) <= self.rows
              or data['has_next'] and len(data['items']) != self.rows
              or not data['has_next'] and max(numbers) != page):
            raise ValueError('UN Geneva current/next/final pager mismatch')
        if any(_date(i['listed_date']) is None for i in data['items']):
            raise UnsupportedSource('ungeneva_listing_datetime_unparseable')
        return data

    def prepare(self, item):
        c = self.crawler
        existing = c._ung_row(item, required=False)
        paper, excluded = self.native(c._ung_detail, item, detail=True)
        if paper is None:
            if not excluded or existing is not None:
                raise ValueError('UN Geneva missing body cannot silently replace a stored document')
            return None, {'item': item, 'fingerprint': excluded, 'reason': 'native_short_body_exclusion'}
        return paper, None

    def fetch(self, cursor, direction):
        incremental = direction == 'newest'
        state = copy.deepcopy(cursor) if cursor else {'page': 0, 'seen': [],
            'exclusions': copy.deepcopy(self.prior_exclusions) if incremental else {}, 'date_up': False}
        page = state['page']
        if cursor and self.read(page-1)['page_hash'] != state['anchor']:
            raise ValueError('UN Geneva previous source page changed')
        data = self.read(page)
        entries = []
        for item in data['items']:
            key = item['key']
            if key in state['seen']:
                raise ValueError('UN Geneva repeated cross-page membership; reconcile snapshot')
            state['seen'].append(key)
            entry = Entry(key, item, item['listed_date'])
            if not incremental or not Store(self.crawler._conn, self.crawler.site_id).known(key) or not self.exists(entry):
                paper, excluded = self.prepare(item)
                if excluded:
                    state['exclusions'][key] = excluded
                    continue
                state['exclusions'].pop(key, None)
                item['_prepared_paper'] = paper
            entries.append(entry)
        dates = [_date(i['listed_date']) for i in data['items']]
        state['date_up'] |= any(b > a for a, b in zip(dates, dates[1:]))
        if cursor and dates[0] > state['last_date']:
            state['date_up'] = True
        self.can_read_newest = not state['date_up']
        exhausted = not data['has_next']
        if exhausted:
            self.read(page+1, empty_after=page)
        state.update(page=page+1, anchor=data['page_hash'], last_date=dates[-1])
        return Page(entries, state, exhausted,
                    'native final pager and adjacent empty catalogue; exclusions recorded' if exhausted else
                    'source page contains native exclusions only' if not entries else '',
                    checkpoint_only=not entries and not exhausted)

    def exists(self, entry):
        return self.crawler._ung_row(entry.payload, required=False) is not None

    def materialize(self, item):
        c = self.crawler
        for excluded in self.resume_exclusions.values():
            self.native(c._ung_excluded_prefix, excluded['item'], excluded['fingerprint'], detail=True)
        self.resume_exclusions = {}
        c._ung_row(item, required=False)
        if '_prepared_paper' not in item:
            raise ValueError('UN Geneva uncached delivery item')
        return item['_prepared_paper']
