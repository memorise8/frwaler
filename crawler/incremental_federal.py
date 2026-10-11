"""Federal Register HHS adapter with durable, disjoint publication-date ranges.

Uses the existing date partition and checked provider links. A date leaf never
certifies the entire source: all leaf counts must reconcile to the observed
unbounded count. A changed source fails closed, retaining the staged queue.
"""
from __future__ import annotations
from datetime import date
import hashlib
from pathlib import Path
from urllib.parse import urlencode

from .incremental import Entry, Page, encoded
from .incremental_adapters import Adapter
from .federal_checkpoint import digest
from delivery.health.federal_date_plan import partition


class FederalAdapter(Adapter):
    leaf_limit = 1500

    def __init__(self, crawler, *, lower=None, upper=None):
        if (lower is None) != (upper is None):
            raise ValueError('both date bounds are required')
        if lower is not None and (date.fromisoformat(lower).isoformat() != lower
                                  or date.fromisoformat(upper).isoformat() != upper or lower > upper):
            raise ValueError('invalid Federal date scope')
        super().__init__(crawler, {'endpoint': crawler._API_BASE, 'agency': crawler._AGENCY_SLUG,
                                   'search_type_id': '3', 'lower': lower, 'upper': upper,
                                   'rows': crawler._PAGE_SIZE, 'leaf_limit': self.leaf_limit,
                                   'identity': 'publisher_number_and_original_url'})
        self.contract = hashlib.sha256(self.contract.encode() + Path(__file__).read_bytes()).hexdigest()

    def url(self, *, lower=None, upper=None, order='newest'):
        pairs = [('conditions[agencies][]', self.scope['agency']), ('conditions[search_type_id]', '3'),
                 ('order', order), ('per_page', str(self.scope['rows'])), ('page', '1')]
        if lower is not None:
            pairs.extend([('conditions[publication_date][gte]', lower), ('conditions[publication_date][lte]', upper)])
        return self.scope['endpoint'] + '?' + urlencode(pairs)

    @staticmethod
    def key(item):
        number, url = item.get('document_number'), item.get('html_url')
        if not isinstance(number, str) or not number.strip() or not isinstance(url, str) or not url.strip():
            raise ValueError('Federal identity missing')
        return digest((number.strip(), url.strip()))

    def read(self, url, *, lower=None, upper=None, order='newest'):
        data = self.get(url)
        docs, count = data.get('results'), data.get('count')
        if (not isinstance(docs, list) or type(count) is not int or count < 0
                or len(docs) > min(self.scope['rows'], count) or (count > 0 and not docs)):
            raise ValueError('invalid Federal listing or count')
        days, keys = [], []
        for item in docs:
            keys.append(self.key(item))
            day = date.fromisoformat(item['publication_date']).isoformat()
            if (lower and day < lower) or (upper and day > upper):
                raise ValueError('Federal date filter ignored')
            days.append(day)
        if days != sorted(days, reverse=order == 'newest') or len(keys) != len(set(keys)):
            raise ValueError('Federal order or identities inconsistent')
        return data

    def fetch(self, cursor, direction):
        # Both modes request newest explicitly, but eligibility is inferred by
        # Collector from the completed stream, never from this declaration.
        if cursor is None:
            lower, upper = self.scope['lower'], self.scope['upper']
            newest = self.read(self.url(lower=lower, upper=upper), lower=lower, upper=upper)
            if newest['count'] == 0:
                return Page([], None, True, 'validated zero source count')
            if lower is None:
                oldest = self.read(self.url(order='oldest'), order='oldest')
                if oldest['count'] != newest['count']:
                    raise ValueError('Federal source changed during bound observation')
                lower = oldest['results'][0]['publication_date']
                upper = newest['results'][0]['publication_date']
            cursor = {'expected_total': newest['count'], 'count_is_lower_bound': newest['count'] >= 10000, 'finished_count': 0,
                      'ranges': [{'lower': lower, 'upper': upper}], 'leaf': None}
            return Page([], cursor, evidence='validated publication bounds and count', checkpoint_only=True)

        cursor = dict(cursor, ranges=[dict(r) for r in cursor['ranges']])
        leaf = dict(cursor['leaf']) if cursor.get('leaf') else None
        if leaf:
            # A saved partial leaf is bound to its last original page. This
            # rejects offset shifts after a pause, instead of skipping records.
            anchor = self.read(leaf['anchor_url'], lower=leaf['lower'], upper=leaf['upper'])
            if digest(anchor) != leaf['anchor_hash']:
                raise ValueError('Federal leaf anchor changed')
            data = self.read(leaf['next_url'], lower=leaf['lower'], upper=leaf['upper'])
            if data['count'] != leaf['count']:
                raise ValueError('Federal leaf count changed')
            request_url = leaf['next_url']
        else:
            bounds = cursor['ranges'][0]
            request_url = self.url(**bounds)
            data = self.read(request_url, **bounds)
            count = data['count']
            if count >= self.leaf_limit:
                children = partition(**bounds)
                # Newest date partition first; no gaps and no overlap.
                cursor['ranges'] = list(reversed(children)) + cursor['ranges'][1:]
                return Page([], cursor, evidence='source count requires disjoint date subdivision', checkpoint_only=True)
            leaf = dict(bounds, count=count, seen=[], next_url=None)

        docs = data['results']
        keys = [self.key(item) for item in docs]
        if set(keys).intersection(leaf['seen']):
            raise ValueError('Federal repeated source records')
        leaf['seen'] = leaf['seen'] + keys
        if len(leaf['seen']) > leaf['count']:
            raise ValueError('Federal leaf exceeds advertised count')
        next_url = data.get('next_page_url')
        if next_url:
            next_url = self.crawler._checked_next_url(next_url, date_range=(leaf['lower'], leaf['upper']))
            if next_url == request_url or len(leaf['seen']) >= leaf['count']:
                raise ValueError('Federal invalid continuation')
            leaf.update(next_url=next_url, anchor_url=request_url, anchor_hash=digest(data))
            cursor['leaf'] = leaf
        else:
            if len(leaf['seen']) != leaf['count']:
                raise ValueError('Federal premature end of leaf')
            cursor['finished_count'] += leaf['count']
            cursor['ranges'] = cursor['ranges'][1:]
            cursor['leaf'] = None
        exhausted = not cursor['ranges']
        if exhausted:
            valid_count = (cursor['finished_count'] >= cursor['expected_total'] if cursor['count_is_lower_bound']
                           else cursor['finished_count'] == cursor['expected_total'])
            if not valid_count:
                raise ValueError('Federal leaf union does not match source count evidence')
        return Page([Entry(self.key(d), d, d['publication_date']) for d in docs], cursor, exhausted,
                    'all observed date leaves exhausted; capped source count treated as lower bound' if exhausted else 'validated date leaf',
                    checkpoint_only=not docs and not exhausted)

    def exists(self, entry):
        from . import db_libertree, db_pg
        c = self.crawler
        number = entry.payload['document_number'].strip()
        url = entry.payload['html_url'].strip()
        if type(c._conn).__module__.startswith('sqlite3'):
            if c._conn_is_libertree():
                return db_libertree.find_by_dedup_key(c._conn, c.site_id, number, url) is not None
            return c._conn.execute('SELECT 1 FROM documents WHERE site_id=? AND external_id=? AND meta_url=?',
                                   (c.site_id, number, url)).fetchone() is not None
        return db_pg.find_by_dedup_key(c._conn, c.site_id, number, url) is not None

    def materialize(self, d):
        # Keep original identity/URLs and raw short or empty descriptions. Do
        # not manufacture minimum-length text or omit source records.
        pdf = d.get('pdf_url') or None
        return {'site_id': self.crawler.site_id, 'external_id': d['document_number'].strip(),
                'url': d['html_url'].strip(), 'title': d.get('title') or '(untitled)',
                'abstract': d.get('abstract') or d.get('excerpts') or '',
                'published_date': d['publication_date'], 'posted_date': d['publication_date'],
                'publisher': '; '.join(a['name'] for a in d.get('agencies',[]) if a.get('name')),
                'keywords': ', '.join(str(t) for t in d.get('topics',[])),
                'pdf_url': pdf, 'original_filename': pdf.rsplit('/',1)[-1].split('?')[0] if pdf else None,
                'metadata': encoded(d)}
