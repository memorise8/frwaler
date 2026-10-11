"""Both delivered BfR catalogues with verified per-scope overlap boundaries."""
import copy
import hashlib
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from .document_dates import normalize_date
from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter

SCOPES = {'annual': {'url': 'https://www.bfr.bund.de/en/publications/annual-reports/',
                     'filters': ['folder:117', 'folder:1179', 'folder:3746']},
          'scientific': {'url': 'https://www.bfr.bund.de/en/publications/scientific-reports/',
                         'filters': ['folder:110']}}


def day(item):
    d = normalize_date(item['raw']['publishingDate'], order='DMY')
    if d['status'] != 'normalized' or d['precision'] != 'day':
        raise UnsupportedSource('bfr_precise_source_date_unavailable')
    return d['value']


class BfRAdapter(Adapter):
    rows = 6

    def __init__(self, crawler, *, scopes=None):
        selected = list(scopes) if scopes is not None else list(SCOPES)
        if not selected or len(set(selected)) != len(selected) or not set(selected) <= set(SCOPES):
            raise ValueError('unknown BfR delivered scope')
        super().__init__(crawler, {'scopes': selected, 'config': SCOPES, 'rows': self.rows,
                                 'endpoint': crawler.FALLBACK_API_ENDPOINT})
        self.contract = hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()
        self.config_checked = False
        state = Store(crawler._conn, crawler.site_id).state() if crawler._conn is not None else {}
        same = state.get('contract') == self.contract
        self.prior_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'idle' and state.get('baseline_complete') else {})
        self.resume_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'deliver' else {})

    def response(self, url, *, detail=False):
        target = urlsplit(url)
        if target.scheme != 'https' or target.netloc != 'www.bfr.bund.de':
            raise UnsupportedSource('bfr_resource_outside_reviewed_publisher_scope')
        self.http_requests += 1
        if detail:
            self.detail_requests += 1
        r = self.crawler._request(url, retries=1, headers={'Accept-Encoding': 'gzip, deflate'})
        if r is None:
            raise ConnectionError('BfR source response unavailable')
        return r

    def check_config(self):
        if self.config_checked:
            return
        c = self.crawler
        for name in self.scope['scopes']:
            spec = SCOPES[name]
            soup = c._make_soup(self.response(spec['url']).text)
            if soup is None or soup.select_one('#search-app[data-endpoint][data-restriction-filters]') is None:
                raise ValueError('BfR source catalogue configuration missing')
            endpoint, filters = c._discover_search_config(soup)
            if endpoint != self.scope['endpoint'] or filters != spec['filters']:
                raise ValueError('BfR source catalogue scope changed')
        self.config_checked = True

    def url(self, scope, page):
        params = [('tx_solr[filter][]', v) for v in SCOPES[scope]['filters']]
        if page > 1:
            params.append(('tx_solr[page]', str(page)))
        return self.scope['endpoint']+'?'+urlencode(params)

    def raw_page(self, scope, page):
        self.check_config()
        data = self.response(self.url(scope, page)).json()['content']['colPos0'][0]['content']['data']
        total, pager = data['allResultCount'], data['pagination']
        raw = data['documents']['list']['results']
        if type(total) is not int or total < 0 or not isinstance(raw, list):
            raise ValueError('BfR invalid source total or rows')
        last = max(1, (total+self.rows-1)//self.rows)
        expected = max(0, min(self.rows, total-(page-1)*self.rows))
        if (pager != {'current': page, 'numberOfPages': last} or len(raw) != expected
                or data['documents']['list']['count'] != len(raw)):
            raise ValueError('BfR current page/total/row count mismatch')
        selected = data['facetsActive']['selected']
        if ([f"{f['name']}:{f['item']}" for f in selected] != SCOPES[scope]['filters']):
            raise ValueError('BfR source ignored catalogue filter')
        items = self.crawler._parse_list_records(data)
        if len(items) != len(raw) or any(not i['external_id'].isdecimal() or not i['url'] for i in items):
            raise UnsupportedSource('bfr_incomplete_stable_source_identity')
        if len({i['external_id'] for i in items}) != len(items):
            raise UnsupportedSource('bfr_repeated_source_uid')
        dates = [day(i) for i in items]
        if dates != sorted(dates, reverse=True):
            raise UnsupportedSource('bfr_source_date_order_ambiguous')
        return items, total, last

    def read(self, scope, index):
        items, total, last = self.raw_page(scope, index+1)
        final = index+1 == last
        if final:
            empty, end_total, end_last = self.raw_page(scope, last+1)
            if empty or (end_total, end_last) != (total, last):
                raise ValueError('BfR adjacent boundary contradicts final page')
        return {'items': items, 'next_url': None if final else self.url(scope, index+2),
                'last_page': last, 'page_hash': hashlib.sha256(encoded([items, total, last]).encode()).hexdigest()}

    def assert_identity(self, item):
        db = Store(self.crawler._conn, self.crawler.site_id)
        rows = db.execute('SELECT post_number,meta_url FROM documents WHERE site_id=? AND (post_number=? OR meta_url=?)',
                          (self.crawler.site_id, item['external_id'], item['url'])).fetchall()
        if len(rows) > 1 or rows and dict(rows[0]) != {'post_number': item['external_id'], 'meta_url': item['url']}:
            raise UnsupportedSource('bfr_stored_identity_collision')

    def fetch(self,cursor,direction):
        incremental=direction=='newest'
        if cursor is None:
            cursor={'streams':[{'scope':s,'page':0,'buffer':[],'done':False,'last_page':None,'anchor':None,
                                'seen':[],'last_date':None,'known_pages':0,'unknown_ids':[]} for s in self.scope['scopes']],
                    'identities':{},'exclusions':copy.deepcopy(self.prior_exclusions) if incremental else {}}
        else:cursor=copy.deepcopy(cursor)
        if [s['scope'] for s in cursor['streams']]!=self.scope['scopes']:raise ValueError('BfR cursor scope mismatch')
        entries=[]
        while len(entries)<self.rows:
            for stream in cursor['streams']:
                if stream['done'] or (stream['buffer'] and (not incremental or stream['page']>=2)):continue
                scope,page=stream['scope'],stream['page']
                if stream['anchor'] is not None:
                    previous=self.read(scope,page-1)
                    if previous['page_hash']!=stream['anchor'] or previous['last_page']!=stream['last_page']:
                        raise ValueError('BfR previous page changed')
                data=self.read(scope,page);items=data['items'];ids=[x['external_id'] for x in items]
                if stream['last_page'] is not None and data['last_page']!=stream['last_page']:raise ValueError('BfR pager boundary changed')
                if set(ids).intersection(stream['seen']):raise ValueError('BfR repeated source page')
                if items and stream['last_date'] is not None and day(items[0])>stream['last_date']:
                    raise UnsupportedSource('bfr_cross_page_date_order_changed')
                unknown=[]
                for item in items:
                    key=item['external_id']
                    if key in cursor['identities'] and cursor['identities'][key]!=item['url']:
                        raise UnsupportedSource('bfr_cross_scope_slug_collision')
                    cursor['identities'][key]=item['url'];self.assert_identity(item)
                    if incremental and (not Store(self.crawler._conn,self.crawler.site_id).known(key) or not self.exists(Entry(key,item))):unknown.append(key)
                stream['seen'].extend(ids);stream['unknown_ids'].extend(unknown)
                stream.update(buffer=stream['buffer']+[dict(x,_source_scope=scope) for x in items],page=page+1,
                              done=data['next_url'] is None,last_page=data['last_page'],anchor=data['page_hash'],
                              last_date=day(items[-1]) if items else stream['last_date'],
                              known_pages=stream['known_pages']+1 if incremental and items and not unknown else 0)
            candidates=[s for s in cursor['streams'] if s['buffer']]
            if not candidates:break
            stream=max(candidates,key=lambda s:(day(s['buffer'][0]),s['buffer'][0]['external_id']))
            item=stream['buffer'].pop(0);key=item['external_id']
            if key in stream['unknown_ids']:stream['unknown_ids'].remove(key)
            if not incremental or not Store(self.crawler._conn,self.crawler.site_id).known(key) or not self.exists(Entry(key,item)):
                paper, exclusion = self.prepare(item)
                if exclusion:
                    cursor['exclusions'][key] = {'item':item,'fingerprint':exclusion,'reason':'native_short_body_without_PDF'}
                    continue
                cursor['exclusions'].pop(key,None)
                item['_prepared_paper'] = paper
            entries.append(Entry(key,item,day(item)))
        exhausted=all(s['done'] and not s['buffer'] for s in cursor['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in cursor['streams'])
        return Page(entries,cursor,exhausted,'both original scope pagers exhausted' if exhausted else '',boundary_safe=safe if incremental else True)


    def prepare(self, item):
        self.assert_identity(item)
        c = self.crawler
        source = {}
        text, binary = c._curl_get_text, c._curl_get_bytes
        def fetch_text(url, **kwargs):
            r = self.response(url, detail=True)
            soup = c._make_soup(r.text)
            if soup is None or soup.select_one('h1') is None:
                raise ValueError('BfR native detail shape missing')
            source['parsed'] = c._parse_detail(soup,item)
            return r.text, url
        def fetch_bytes(url, **kwargs):
            r = self.response(url)
            if not r.content.startswith(b'%PDF-'):
                raise UnsupportedSource('bfr_download_archive_not_pdf_requires_primary_member_contract')
            return r.content, url
        c._curl_get_text, c._curl_get_bytes = fetch_text, fetch_bytes
        try:
            paper = c._materialize_record(item, self.scope['endpoint'], SCOPES[item['_source_scope']]['filters'])
        finally:
            c._curl_get_text, c._curl_get_bytes = text, binary
        if paper is None:
            if (item['pdf_url'] or source.get('parsed',{}).get('pdf_url')
                    or not source or self.exists(Entry(item['external_id'],item))):
                raise ValueError('BfR native body/PDF gap; pending retained')
            return None, hashlib.sha256(encoded([item['external_id'],item['url'],day(item),source,
                c.PREFERRED_ABSTRACT_CHARS]).encode()).hexdigest()
        if paper['external_id'] != item['external_id'] or paper['url'] != item['url']:
            raise UnsupportedSource('bfr_materialized_identity_changed')
        return paper, None

    def materialize(self, item):
        for excluded in self.resume_exclusions.values():
            paper, fingerprint = self.prepare(excluded['item'])
            if paper is not None or fingerprint != excluded['fingerprint']:
                raise ValueError('BfR exclusion evidence changed; pending retained')
        self.resume_exclusions = {}
        self.assert_identity(item)
        if '_prepared_paper' not in item:
            raise ValueError('BfR uncached delivery item')
        return item['_prepared_paper']
