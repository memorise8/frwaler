"""ONERA's native French press list and PDF-enriched materializer."""
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from .incremental import Entry, Page, Store, UnsupportedSource
from .incremental_adapters import Adapter


class OneraAdapter(Adapter):
    rows = 12
    view_selector = '.view-id-espace_presse.view-display-id-liste_communiques'

    def __init__(self, crawler):
        super().__init__(crawler, {'endpoint': crawler.base_url+'/fr/presse', 'rows': self.rows})
        self.contract = hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('onera_traversal.py').read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()

    def html(self, url, *, detail=False):
        self.http_requests += 1
        if detail:
            self.detail_requests += 1
        r = self.crawler._request(url, retries=1, headers={'Accept-Encoding': 'gzip, deflate'})
        if r is None:
            raise ConnectionError('ONERA response unavailable')
        return r.text

    def read(self, page, *, allow_empty=False):
        c = self.crawler
        url = self.scope['endpoint']+'?page='+str(page)
        raw = self.html(url)
        soup = c._soup(raw, context='incremental press listing')
        if soup is None or len(soup.select(self.view_selector)) != 1:
            raise ValueError('ONERA original press view missing or ambiguous')
        view = soup.select_one(self.view_selector)
        items = c._parse_list(view)
        if not items:
            if (not allow_empty or view.select('[class*="home-card"]')
                    or view.select('a[rel="next"]') or view.select_one('.view-empty') is None):
                raise ValueError('ONERA empty view lacks explicit end evidence')
            return [], None, None
        native = c._fetch
        def retained(target, **kwargs):
            if target != url:
                raise ValueError('ONERA native listing changed scope')
            return raw
        c._fetch = retained
        try:
            data = c._press_page(page)
        finally:
            c._fetch = native
        if data['items'] != items or not 1 <= len(items) <= self.rows:
            raise ValueError('ONERA original press list/card mismatch')
        if data['next_page'] is not None and len(items) != self.rows:
            raise ValueError('ONERA nonfinal page has unexpected row count')
        if any(not i['listed_date'] for i in items):
            raise UnsupportedSource('onera_precise_list_date_unavailable')
        # Do not assume the declared legacy order; Collector proves all entries.
        return items, data['next_page'], data['page_hash']

    def fetch(self, cursor, direction):
        page = cursor['page'] if cursor else 0
        if cursor:
            _, _, anchor = self.read(page-1)
            if anchor != cursor['anchor']:
                raise ValueError('ONERA previous listing changed; reconcile required')
        items, next_page, anchor = self.read(page)
        keys = [i['url'] for i in items]
        if cursor and set(keys).intersection(cursor['seen']):
            raise ValueError('ONERA repeated cross-page source membership')
        exhausted = next_page is None
        if exhausted:
            following, _, _ = self.read(page+1, allow_empty=True)
            if following:
                raise ValueError('ONERA missing next link contradicted by adjacent records')
        dates = [i['listed_date'] for i in items]
        up = bool((cursor or {}).get('date_up')) or any(b > a for a, b in zip(dates, dates[1:]))
        if cursor and dates[0] > cursor['last_date']:
            up = True
        # This public route has only a forward pager. If first traversal proves
        # oldest-first, keep full rescans instead of pretending page zero is new.
        self.can_read_newest = not up
        state = {'page': page+1, 'anchor': anchor, 'seen': (cursor or {}).get('seen', [])+keys,
                 'last_date': dates[-1], 'date_up': up}
        return Page([Entry(i['url'], i, i['listed_date']) for i in items], state, exhausted,
                    'native final pager plus adjacent explicit empty press view' if exhausted else '')

    def stored(self, url, identity=None):
        c = self.crawler
        db = Store(c._conn, c.site_id)
        modern = not db.sqlite or c._conn_is_libertree()
        url_col, id_col = ('meta_url', 'post_number') if modern else ('url', 'external_id')
        query = f'SELECT {url_col} AS source_url,{id_col} AS source_id FROM documents WHERE site_id=? AND ({url_col}=?'
        args = [c.site_id, url]
        if identity is not None:
            query += f' OR {id_col}=?'
            args.append(identity)
        rows = db.execute(query+')', tuple(args)).fetchall()
        if len(rows) > 1:
            raise UnsupportedSource('onera_ambiguous_stored_identity')
        row = dict(rows[0]) if rows else None
        if row and (row['source_url'] != url or identity is not None and row['source_id'] != identity):
            raise UnsupportedSource('onera_changed_stored_identity')
        return row

    def exists(self, entry):
        return self.stored(entry.key) is not None

    def materialize(self, item):
        c = self.crawler
        native_fetch, native_request = c._fetch, c._session.request
        c._fetch = lambda url, **kwargs: self.html(url, detail=True)
        def counted(method, url, **kwargs):
            if urlsplit(url).path.lower().endswith('.pdf'):
                self.http_requests += 1
                kwargs.setdefault('headers', {})['Accept-Encoding'] = 'gzip, deflate'
            return native_request(method, url, **kwargs)
        c._session.request = counted
        try:
            paper = c._materialize_item(item)
        finally:
            c._fetch, c._session.request = native_fetch, native_request
        if paper is None or c.delivery_source_failure:
            raise ValueError('ONERA native body unavailable; pending retained')
        if paper['url'] != item['url'] or not paper['external_id'].isdecimal():
            raise UnsupportedSource('onera_native_node_or_canonical_identity_unverified')
        self.stored(item['url'], paper['external_id'])
        return paper
