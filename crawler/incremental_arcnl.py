"""Three ARCNL thesis catalogues, merged before applying existing-item stops."""
import copy
from datetime import date
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter

BASE = 'https://arcnl.nl'
SCOPES = ('bachelorthesis', 'masterthesis', 'phdthesis')
LABELS = dict(zip(SCOPES, ('Bachelor Thesis', 'Master Thesis', 'Theses')))


def list_url(scope, page):
    if scope not in SCOPES or page < 1:
        raise ValueError('ARCNL invalid catalogue or page')
    return BASE + '/publications/types/' + scope + (f'/page/{page}' if page > 1 else '')


class ARCNLAdapter(Adapter):
    rows = 20

    def __init__(self, crawler):
        super().__init__(crawler, dict(scopes=[list_url(s, 1) for s in SCOPES],
                                      identity='original_URL_with_native_postid'))
        self.native = crawler.crawl.__globals__
        paths = [Path(__file__), Path(crawler.crawl.__code__.co_filename),
                 Path(__file__).with_name('document_dates.py')]
        self.contract = hashlib.sha256(self.contract.encode() + b''.join(p.read_bytes() for p in paths)).hexdigest()

    def response(self, url, *, detail=False):
        p = urlsplit(url)
        if p.scheme != 'https' or p.netloc != 'arcnl.nl' or p.query or p.fragment:
            raise UnsupportedSource('arcnl_resource_outside_original_host')
        self.http_requests += 1
        self.detail_requests += int(detail)
        r = self.crawler._session.get(url, timeout=40, allow_redirects=False,
                                     headers={'Accept-Encoding': 'gzip, deflate'})
        if r.status_code != 200:
            r.raise_for_status()
            raise ValueError('ARCNL unexpected status or redirect')
        from delivery.health.access_challenge import access_challenge
        if access_challenge(r.text) or '</html>' not in r.text.lower():
            raise ValueError('ARCNL incomplete or denied source')
        return r

    def inspect(self, raw, scope, page, *, empty=False):
        soup = self.native['_make_soup'](raw)
        if soup is None or soup.body is None:
            raise ValueError('ARCNL missing page structure')
        headings = [h.get_text(' ', strip=True) for h in soup.select('h1')]
        if headings != ['Publications - ' + LABELS[scope]]:
            raise ValueError('ARCNL catalogue heading changed')
        classes = soup.body.get('class') or []
        if page > 1 and 'paged-' + str(page) not in classes:
            raise ValueError('ARCNL returned another page')
        if empty:
            content = soup.select('.block__content')
            if (soup.select('.item--publication,.page-numbers') or
                    not any(c.get_text(' ', strip=True) == 'No items found.' for c in content)):
                raise ValueError('ARCNL adjacent source boundary is not explicit empty')
            return [], page - 1
        if 'term-' + scope not in classes:
            raise ValueError('ARCNL category changed')
        canonical = soup.select('link[rel="canonical"]')
        if len(canonical) != 1 or canonical[0].get('href', '').rstrip('/') != list_url(scope, page):
            raise ValueError('ARCNL list canonical changed')
        cards = soup.select('.item--publication')
        if not 1 <= len(cards) <= 10:
            raise ValueError('ARCNL list cardinality changed')
        items = []
        for card in cards:
            links = card.select('.item__title a[href]')
            meta = card.select('meta[itemprop="url"]')
            if len(links) != 1 or len(meta) != 1 or meta[0].get('content') != links[0]['href']:
                raise UnsupportedSource('arcnl_listing_identity_unverified')
            a = links[0]; url = a['href']; p = urlsplit(url)
            if p.scheme != 'https' or p.netloc != 'arcnl.nl' or p.query or p.fragment or not re.fullmatch(r'/publications/[^/]+', p.path):
                raise UnsupportedSource('arcnl_listing_identity_unverified')
            reference = a.get_text(' ', strip=True)
            m = re.search(r', (\d{4}-\d{2}-\d{2})$', reference)
            try:
                stamp = date.fromisoformat(m.group(1)).isoformat() if m else None
                if stamp is None: raise ValueError('missing date')
            except ValueError:
                raise UnsupportedSource('arcnl_precise_listing_date_unavailable')
            title = a.find('em')
            if title is None or not title.get_text(strip=True):
                raise ValueError('ARCNL title structure changed')
            items.append(dict(url=url, title=title.get_text(' ', strip=True), reference=reference,
                              listed_date=stamp, _scope=scope, _list_url=list_url(scope, page)))
        if len({i['url'] for i in items}) != len(items):
            raise UnsupportedSource('arcnl_duplicate_list_identity')
        current = soup.select('.page-numbers.current')
        numbers = soup.select('.page-numbers')
        if numbers:
            if len(current) != 1 or current[0].get_text(strip=True) != str(page):
                raise ValueError('ARCNL current page mismatch')
            numeric = [int(n.get_text(strip=True)) for n in numbers if n.get_text(strip=True).isdigit()]
            last = max(numeric)
            for a in soup.select('a.page-numbers'):
                text = a.get_text(strip=True)
                expected = int(text) if text.isdigit() else page + (1 if 'next' in a.get('class', []) else -1)
                if a.get('href', '').rstrip('/') not in {list_url(scope, expected), BASE + '/publications/types/' + scope + '/page/1' if expected == 1 else list_url(scope, expected)}:
                    raise ValueError('ARCNL pager scope or step changed')
            nxt = soup.select('a.next.page-numbers')
            if bool(nxt) != (page < last) or page < last and len(items) != 10:
                raise ValueError('ARCNL inconsistent last page')
        else:
            if page != 1: raise ValueError('ARCNL missing pager')
            last = 1
        return items, last

    def boundary(self, scope, page):
        self.inspect(self.response(list_url(scope, page)).text, scope, page, empty=True)

    def read(self, scope, page):
        raw = self.response(list_url(scope, page)).text
        items, last = self.inspect(raw, scope, page)
        if page == last: self.boundary(scope, page + 1)
        return items, last, hashlib.sha256(encoded(sorted(items, key=lambda i: i["url"])).encode()).hexdigest()

    def stored(self,url,identity=None):
        c=self.crawler
        if c._conn is None:return None
        db=Store(c._conn,c.site_id);modern=not db.sqlite or c._conn_is_libertree()
        id_col,url_col=('post_number','meta_url') if modern else ('external_id','url')
        # Match full original URLs while retaining native WordPress post IDs.
        query=f'SELECT {id_col} AS source_id,{url_col} AS source_url FROM documents WHERE site_id=? AND {url_col}=?'
        rows=db.execute(query,(c.site_id,url)).fetchall()
        if len(rows)>1 or rows and (rows[0]['source_url']!=url or identity is not None and str(rows[0]['source_id'])!=str(identity)):
            raise UnsupportedSource('arcnl_native_ID_collision_or_change')
        return dict(rows[0]) if rows else None

    def exists(self,entry):return self.stored(entry.key) is not None

    def fetch(self,cursor,direction):
        state=copy.deepcopy(cursor) if cursor else dict(streams=[dict(scope=s,page=1,buffer=[],done=False,last=None,
            anchor=None,seen=[],last_date=None,date_up=False,known_pages=0,unknown_ids=[]) for s in SCOPES],emitted={})
        if [s['scope'] for s in state['streams']]!=list(SCOPES):raise ValueError('ARCNL cursor scope changed')
        incremental=direction=='newest';output=[]
        while len(output)<self.rows:
            for stream in state['streams']:
                if stream['done'] or stream['buffer'] and (not incremental or stream['page']>=3):continue
                scope,page=stream['scope'],stream['page']
                if stream['anchor'] is not None:
                    _,last,anchor=self.read(scope,page-1)
                    if last!=stream['last'] or anchor!=stream['anchor']:raise ValueError('ARCNL previous page drift; pending retained')
                items,last,anchor=self.read(scope,page);keys=[i['url'] for i in items];dates=[i['listed_date'] for i in items]
                if stream['last'] is not None and last!=stream['last']:raise ValueError('ARCNL last page changed')
                if set(keys).intersection(stream['seen']):raise UnsupportedSource('arcnl_cross_page_duplicate_identity')
                stream['date_up']|=any(b>a for a,b in zip(dates,dates[1:]))
                if stream['last_date'] is not None and dates[0]>stream['last_date']:stream['date_up']=True
                if incremental and stream['date_up']:raise UnsupportedSource('arcnl_listing_order_changed')
                unknown=[i['url'] for i in items if incremental and
                    (not Store(self.crawler._conn,self.crawler.site_id).known(i['url']) or not self.exists(Entry(i['url'],i)))]
                stream['seen']+=keys;stream['unknown_ids']+=unknown
                stream.update(buffer=stream['buffer']+items,page=page+1,done=page==last,last=last,anchor=anchor,
                    last_date=dates[-1],known_pages=stream['known_pages']+1 if incremental and not unknown else 0)
            active=[s for s in state['streams'] if s['buffer']]
            if not active:break
            # First scope wins shared URLs at identical dates, as in native crawl.
            stream=max(active,key=lambda s:(s['buffer'][0]['listed_date'],-SCOPES.index(s['scope'])))
            item=stream['buffer'].pop(0);key=item['url']
            if key in stream['unknown_ids']:stream['unknown_ids'].remove(key)
            if key in state['emitted']:
                if state['emitted'][key]!=item['listed_date']:raise UnsupportedSource('arcnl_overlap_date_conflict')
                continue
            state['emitted'][key]=item['listed_date'];output.append(Entry(key,item,item['listed_date']))
        self.can_read_newest=not any(s['date_up'] for s in state['streams'])
        exhausted=all(s['done'] and not s['buffer'] for s in state['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in state['streams'])
        return Page(output,state,exhausted,'both original catalogues plus adjacent empty responses' if exhausted else '',boundary_safe=safe if incremental else True)

    def materialize(self, item):
        raw = self.response(item['url'], detail=True).text
        soup = self.native['_make_soup'](raw)
        canonical = soup.select('link[rel="canonical"]') if soup else []
        if len(canonical) != 1 or canonical[0].get('href', '').rstrip('/') != item['url']:
            raise ValueError('ARCNL detail identity mismatch')
        data = self.crawler._parse_detail(item['url'], raw=raw)
        if not data or not data.get('post_id') or not data.get('title'):
            raise UnsupportedSource('arcnl_native_detail_identity_unavailable')
        if data['published_date'] != item['listed_date']:
            raise ValueError('ARCNL detail/list date mismatch; pending retained')
        self.stored(item['url'], data['post_id'])
        if len(data.get('abstract', '')) < 50:
            raise UnsupportedSource('arcnl_native_short_abstract_requires_full_rescan')
        return self.crawler._paper(data, item['url'], item['_scope'])
