"""List-level adapters. Unsupported custom parsers remain explicitly classified.

Queries preserve the delivered site scope. In particular INSPIRE identifiers
are not treated as proof of membership in the CERN CDS collection.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from urllib.parse import quote, urlencode

from .incremental import Entry, Page, encoded

HAL_SCOPES = json.loads(Path(__file__).with_name('hal_incremental_scopes.json').read_text())


def joined(value):
    return '; '.join(str(x) for x in value) if isinstance(value, list) else str(value or '')


class Adapter:
    stable_ids = True
    numeric_ids = False
    can_read_newest = True
    detail_requests = 0
    http_requests = 0

    def __init__(self, crawler, scope):
        self.crawler = crawler
        self.scope = scope
        self.contract = hashlib.sha256(encoded({'v': 1, 'adapter': type(self).__name__, 'scope': scope}).encode()
                                       + Path(__file__).read_bytes()).hexdigest()

    def get(self, url, params=None):
        self.http_requests += 1
        response = self.crawler._request(url, params=params, retries=1)
        if response is None:
            raise ValueError('source fetch failed')
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError('invalid API response')
        return data

    def exists(self, entry):
        c = self.crawler
        if type(c._conn).__module__.startswith('sqlite3') and c._conn_is_libertree():
            return c._conn.execute('SELECT 1 FROM documents WHERE site_id=? AND post_number=? LIMIT 1',
                                   (c.site_id, entry.key)).fetchone() is not None
        if type(c._conn).__module__.startswith('sqlite3'):
            return c._conn.execute('SELECT 1 FROM documents WHERE site_id=? AND external_id=? LIMIT 1',
                                   (c.site_id, entry.key)).fetchone() is not None
        return c._conn.execute('SELECT 1 FROM documents WHERE site_id=%s AND post_number=%s LIMIT 1',
                               (c.site_id, entry.key)).fetchone() is not None


class HALAdapter(Adapter):
    # HAL docid is a string in Solr: sorting it yields 999999 before 5792865.
    # Use repository submission date + unique docid and native cursorMark.
    numeric_ids = False
    rows = 100

    def __init__(self, crawler, scope=None):
        super().__init__(crawler, scope or HAL_SCOPES[crawler.site_id])

    def fetch(self, cursor, direction):
        desc = direction == 'newest'
        params = [(k,v) for k,v in self.scope['query_pairs'] if k not in ('start','rows','sort','fl')]
        fields = next(v for k,v in self.scope['query_pairs'] if k == 'fl')
        fields += ',submittedDate_tdate'
        params += [('rows', self.rows), ('sort', 'submittedDate_tdate desc,docid desc' if desc else
                                    'submittedDate_tdate asc,docid asc'),
                   ('fl', fields), ('cursorMark', (cursor or {}).get('mark', '*'))]
        data = self.get(self.scope['endpoint'], params)
        response = data.get('response')
        if (not isinstance(response, dict) or not isinstance(response.get('docs'), list)
                or type(response.get('numFound')) is not int or not isinstance(data.get('nextCursorMark'), str)):
            raise ValueError('invalid HAL cursor response')
        docs, total = response['docs'], response['numFound']
        mark = data['nextCursorMark']
        previous_mark = (cursor or {}).get('mark', '*')
        exhausted = mark == previous_mark
        if (not docs and not exhausted) or (docs and exhausted):
            raise ValueError('HAL cursor did not advance consistently')
        entries = [Entry(str(d.get('docid') or ''), d, d.get('submittedDate_tdate') or '') for d in docs]
        return Page(entries, {'mark': mark}, exhausted,
                    'HAL unchanged cursorMark and empty docs' if exhausted else '')

    def _identity(self, d):
        # Preserve the existing crawler's document identity. The discovery
        # cursor still uses unique docid; the storage adapter historically
        # ignores paper['post_number'] and persists paper['external_id'].
        numeric_sites = {
            'enc-hal-science-search', 'enpc-hal-science-search',
            'envt-hal-science-search', 'hec-hal-science-search',
            'icp-hal-science-search', 'imt-nord-europe-hal-science-search',
            'inria-hal-science-search', 'isae-ensma-hal-science-search', 'hal-science-ign-ensg',
        }
        if self.crawler.site_id == 'cnam-hal-science-search' and not d.get('halId_s'):
            return 'docid-' + str(d['docid'])
        return str(d['docid']) if self.crawler.site_id in numeric_sites else str(d.get('halId_s') or d['docid'])

    def exists(self, entry):
        return super().exists(Entry(self._identity(entry.payload), entry.payload))

    def materialize(self, d):
        key = self._identity(d)
        return {'site_id': self.crawler.site_id, 'external_id': key,
                'url': d.get('uri_s') or 'https://hal.science/' + str(d.get('halId_s') or key),
                'title': joined(d.get('title_s')) or '(untitled)',
                'abstract': joined(d.get('abstract_s') or d.get('en_abstract_s')),
                'authors': joined(d.get('authFullName_s')), 'published_date': d.get('producedDate_s'),
                'posted_date': d.get('submittedDate_tdate') or d.get('submittedDate_s'),
                'pdf_url': d.get('fileMain_s'), 'keywords': joined(d.get('keyword_s')),
                'publisher': joined(d.get('publisher_s')), 'journal': joined(d.get('journalTitle_s')),
                'doi': joined(d.get('doiId_s')), 'metadata': encoded(d)}


class OffsetAdapter(Adapter):
    """Recheck the previous page anchor on continuation (also across crashes).

    Drift fails closed with the durable queue retained; no silently shifted
    offset is accepted as completeness evidence.
    """
    size = 50

    def read(self, page):
        raise NotImplementedError

    def fetch(self, cursor, direction):
        page = cursor['page'] if cursor else 1
        if cursor and cursor.get('anchor'):
            previous, _ = self.read(page - 1)
            if hashlib.sha256(encoded(previous).encode()).hexdigest() != cursor['anchor']:
                raise ValueError('source page drift; restart/reconcile required')
        items, total = self.read(page)
        if (type(total) is not int or total < 0 or len(items) > self.size
                or (not items and (page - 1) * self.size < total)):
            raise ValueError('invalid paged response')
        exhausted = (page - 1) * self.size + len(items) == total
        if (page - 1) * self.size + len(items) > total:
            raise ValueError('source total drift')
        return Page([self.entry(x) for x in items],
                    {'page': page + 1, 'anchor': hashlib.sha256(encoded(items).encode()).hexdigest()},
                    exhausted, 'validated API total and last page' if exhausted else '')


class DOAJAdapter(Adapter):
    size = 50
    def __init__(self, crawler, query='*'):
        super().__init__(crawler, {'query': query, 'sort': 'created_date:desc'})

    def fetch(self, cursor, direction):
        # Always page one of a shrinking date/ID query, avoiding the API's
        # deep offset window. Retain EVERY ID tied on the last timestamp.
        query = '(' + self.scope['query'] + ')'
        if cursor:
            date = cursor['date']
            if not date or any(ch not in '0123456789TZ:+-.' for ch in date):
                raise ValueError('invalid DOAJ timestamp cursor')
            ids = cursor['ties']
            if any(not key.isalnum() for key in ids):
                raise ValueError('invalid DOAJ identity cursor')
            query += ' AND created_date:[0001-01-01T00:00:00Z TO ' + date + '] AND NOT id:(' + ' OR '.join(ids) + ')'
        data = self.get('https://doaj.org/api/search/articles/' + quote(query, safe=''),
                        {'pageSize': self.size, 'page': 1, 'sort': self.scope['sort']})
        items, total = data.get('results'), data.get('total')
        if not isinstance(items, list) or type(total) is not int or total < len(items) or (not items and total):
            raise ValueError('invalid DOAJ results')
        entries = [self.entry(x) for x in items]
        if any(not e.date for e in entries) or any(entries[i].date < entries[i+1].date for i in range(len(entries)-1)):
            raise ValueError('DOAJ creation order unverified')
        if cursor and any(e.date > cursor['date'] or e.key in cursor['ties'] for e in entries):
            raise ValueError('DOAJ keyset boundary was ignored')
        next_cursor = cursor
        if entries:
            date = entries[-1].date
            ties = list(cursor['ties']) if cursor and date == cursor['date'] else []
            ties.extend(e.key for e in entries if e.date == date)
            next_cursor = {'date': date, 'ties': sorted(set(ties))}
        exhausted = len(items) == total
        return Page(entries, next_cursor, exhausted, 'DOAJ remaining query exhausted' if exhausted else '')

    def entry(self, d):
        return Entry(str(d.get('id') or ''), d, d.get('created_date') or '')

    def materialize(self, d):
        c, bib = self.crawler, d.get('bibjson') or {}
        return {'site_id': c.site_id, 'external_id': str(d['id']),
                'url': 'https://doaj.org/article/' + str(d['id']), 'title': bib.get('title') or '(untitled)',
                'abstract': bib.get('abstract') or '', 'authors': joined([a.get('name','') for a in bib.get('author',[])]),
                'published_date': c._make_published_date(bib), 'posted_date': d.get('created_date'),
                'pdf_url': c._get_pdf_url(bib), 'doi': c._get_doi(bib),
                'journal': bib.get('journal',{}).get('title'), 'publisher': bib.get('journal',{}).get('publisher'),
                'keywords': joined(bib.get('keywords')), 'metadata': encoded(d)}


class NTRSAdapter(OffsetAdapter):
    # Live API ignores fine-grained created ordering; do not promise a newest end.
    can_read_newest = False
    def __init__(self, crawler, extra=None):
        scope = {'stiTypeDetails': 'Contractor Report (CR)'} if crawler.site_id == 'ntrs-nasa-gov-search' else {}
        scope.update(extra or {})
        super().__init__(crawler, scope)

    def read(self, page):
        params = dict(self.scope)
        params.update({'page[from]': (page-1)*self.size, 'page[size]': self.size,
                       'sort[field]': 'created', 'sort[order]': 'desc'})
        data = self.get('https://ntrs.nasa.gov/api/citations/search', params)
        if not isinstance(data.get('results'), list):
            raise ValueError('invalid NTRS results')
        return data['results'], data.get('stats',{}).get('total')

    def entry(self, d):
        return Entry(str(d.get('id') or ''), d, d.get('created') or '')

    def materialize(self, d):
        from .sites.ntrs import NTRSCrawler
        paper = NTRSCrawler._parse_item(d)
        paper['site_id'] = self.crawler.site_id
        return paper


def adapter_for(crawler):
    if crawler.site_id in HAL_SCOPES:
        return HALAdapter(crawler)
    if crawler.site_id == 'doaj-org-search':
        return DOAJAdapter(crawler)
    if crawler.site_id in ('ntrs', 'ntrs-nasa-gov-search'):
        return NTRSAdapter(crawler)
    # Generic HTML needs an explicit end-of-list selector and stable ID
    # contract; arbitrary empty DOMs cannot certify exhaustion.
    from .generic_crawler import GenericCrawler
    if isinstance(crawler, GenericCrawler) and crawler._config.get('incremental'):
        return HTMLAdapter(crawler)
    return None


class HTMLAdapter(Adapter):
    def __init__(self, crawler):
        cfg = crawler._config
        policy = cfg['incremental']
        if not policy.get('stable_ids') or not policy.get('end_selector'):
            raise ValueError('HTML incremental requires stable IDs and an explicit end selector')
        self.numeric_ids = bool(policy.get('monotonic_numeric_ids'))
        self.can_read_newest = bool(policy.get('newest_params') is not None)
        super().__init__(crawler, cfg)

    def fetch(self, cursor, direction):
        c, cfg = self.crawler, self.scope
        listing, policy = cfg['list_page'], cfg['incremental']
        pagination = listing['pagination']
        step = pagination.get('step', 1)
        number = cursor['page'] if cursor else pagination.get('start', 1)
        params = dict(listing.get('params', {}))
        params.update(policy.get('newest_params', {}) if direction == 'newest' else {})
        params[pagination.get('param', 'page')] = number
        def read(n):
            params[pagination.get('param', 'page')] = n
            self.http_requests += 1
            soup = c._fetch_page(listing['url'], params=params)
            if soup is None:
                raise ValueError('HTML listing unavailable')
            rows = c._extract_list_items(soup, listing['selectors'])
            return soup, rows
        if cursor:
            _, old = read(number-step)
            if hashlib.sha256(encoded(old).encode()).hexdigest() != cursor['anchor']:
                raise ValueError('HTML page drift; reconcile required')
        soup, rows = read(number)
        pinned = {c._make_absolute(t.get('href','')) for t in soup.select(policy.get('pinned_selector') or '.__no_pin__')}
        entries = [Entry(str(r.get('external_id') or ''), r, r.get('published_date') or '',
                         r.get('detail_url') in pinned) for r in rows]
        exhausted = bool(soup.select(policy['end_selector']))
        return Page(entries, {'page': number+step, 'anchor': hashlib.sha256(encoded(rows).encode()).hexdigest()},
                    exhausted, 'configured end selector observed' if exhausted else '')

    def materialize(self, payload):
        c = self.crawler
        detail = self.scope.get('detail_page', {})
        if detail.get('selectors'):
            self.detail_requests += 1
            return c._fetch_detail(payload['detail_url'], payload, detail)
        return c._build_paper_from_list(payload)
