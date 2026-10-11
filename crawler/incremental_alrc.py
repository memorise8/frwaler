"""Merge both delivered ALRC catalogues before applying known-item boundaries."""
import copy
from datetime import date
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .alrc_scope import SCOPES,inspect_listing,list_url
from .incremental import Entry,Page,Store,UnsupportedSource,encoded
from .incremental_adapters import Adapter

BASE='https://www.alrc.gov.au'


class ALRCAdapter(Adapter):
    rows=20

    def __init__(self,crawler):
        super().__init__(crawler,dict(scopes=[list_url(s,1) for s in SCOPES],identity='original_URL_with_native_stored_ID'))
        self.native=crawler._alrc_paper.__globals__
        paths=[Path(__file__),Path(crawler._alrc_paper.__code__.co_filename),Path(crawler.crawl.__code__.co_filename),
               Path(__file__).with_name('alrc_scope.py'),Path(__file__).with_name('document_dates.py')]
        self.contract=hashlib.sha256(self.contract.encode()+b''.join(p.read_bytes() for p in paths)).hexdigest()

    def response(self,url,*,detail=False,allow_404=False):
        p=urlsplit(url)
        if p.scheme!='https' or p.netloc!='www.alrc.gov.au' or p.fragment:
            raise UnsupportedSource('alrc_resource_outside_original_host')
        self.http_requests+=1;self.detail_requests+=int(detail)
        try:
            r=self.crawler._session.get(url,timeout=40,allow_redirects=False,headers={'Accept-Encoding':'gzip, deflate'})
        except requests.HTTPError as exc:
            if exc.response is None or not (allow_404 and exc.response.status_code==404):raise
            r=exc.response
        if r.status_code!=200 and not (allow_404 and r.status_code==404):
            r.raise_for_status();raise ValueError('ALRC unexpected status or redirect')
        from delivery.health.access_challenge import access_challenge
        if access_challenge(r.text) or '</html>' not in r.text.lower():raise ValueError('ALRC incomplete or denied source')
        return r

    def boundary(self,scope,page):
        r=self.response(list_url(scope,page),allow_404=True);soup=self.native['_make_soup'](r.text)
        if soup is None or soup.select('.section-listing-item,.tw-pagination'):
            raise ValueError('ALRC adjacent source boundary contains rows or pager')
        if scope=='final-report':
            main=soup.select_one('#main-content main.content');inner=main.select('.section-listing-inner') if main else []
            if (r.status_code!=200 or len(inner)!=1 or inner[0].find(True) or inner[0].get_text(strip=True)
                    or [h.get_text(' ',strip=True) for h in main.select('h1')]!=['Publications']
                    or [h.get_text(' ',strip=True) for h in main.select('h2.post-subtitle')]!=['Final Report']):
                raise ValueError('ALRC final-report adjacent empty shape changed')
            listing=main.select_one('section.section-listing')
            if not listing or listing.find_all(recursive=False)!=[main.select_one('section.post'),inner[0]]:
                raise ValueError('ALRC unexpected content in empty catalogue')
        elif (r.status_code!=404 or [h.get_text(' ',strip=True) for h in soup.select('h1')]!=['Page not found']
              or 'The requested page was not found.' not in soup.select_one('#main-content').get_text(' ',strip=True)):
            raise ValueError('ALRC search adjacent 404 shape changed')

    def read(self,scope,page):
        raw=self.response(list_url(scope,page)).text;soup=self.native['_make_soup'](raw)
        items,_=self.native['_parse_list_page'](raw);last=inspect_listing(soup,items,scope,page)
        if len(items)>10 or page>last:raise ValueError('ALRC page cardinality changed')
        for item in items:
            p=urlsplit(item['url'])
            if p.scheme!='https' or p.netloc!='www.alrc.gov.au' or p.query or p.fragment:
                raise UnsupportedSource('alrc_listing_identity_unverified')
            try:date.fromisoformat(item['listed_date'])
            except (TypeError,ValueError):raise UnsupportedSource('alrc_precise_listing_date_unavailable')
            item['_scope']=scope;item['_list_url']=list_url(scope,page)
        if len({i['url'] for i in items})!=len(items):raise UnsupportedSource('alrc_duplicate_list_identity')
        if page==last:self.boundary(scope,page+1)
        return items,last,hashlib.sha256(encoded(items).encode()).hexdigest()

    def stored(self,url,identity=None):
        c=self.crawler
        if c._conn is None:return None
        db=Store(c._conn,c.site_id);modern=not db.sqlite or c._conn_is_libertree()
        id_col,url_col=('post_number','meta_url') if modern else ('external_id','url')
        # Native fallback slugs are reused by different chapter URLs. The
        # discovery key is the full original URL; preserve those separate rows.
        query=f'SELECT {id_col} AS source_id,{url_col} AS source_url FROM documents WHERE site_id=? AND {url_col}=?'
        rows=db.execute(query,(c.site_id,url)).fetchall()
        if len(rows)>1 or rows and (rows[0]['source_url']!=url or identity is not None and str(rows[0]['source_id'])!=str(identity)):
            raise UnsupportedSource('alrc_native_ID_collision_or_change')
        return dict(rows[0]) if rows else None

    def exists(self,entry):return self.stored(entry.key) is not None

    def fetch(self,cursor,direction):
        state=copy.deepcopy(cursor) if cursor else dict(streams=[dict(scope=s,page=1,buffer=[],done=False,last=None,
            anchor=None,seen=[],last_date=None,date_up=False,known_pages=0,unknown_ids=[]) for s in SCOPES],emitted={})
        if [s['scope'] for s in state['streams']]!=list(SCOPES):raise ValueError('ALRC cursor scope changed')
        incremental=direction=='newest';output=[]
        while len(output)<self.rows:
            for stream in state['streams']:
                if stream['done'] or stream['buffer'] and (not incremental or stream['page']>=3):continue
                scope,page=stream['scope'],stream['page']
                if stream['anchor'] is not None:
                    _,last,anchor=self.read(scope,page-1)
                    if last!=stream['last'] or anchor!=stream['anchor']:raise ValueError('ALRC previous page drift; pending retained')
                items,last,anchor=self.read(scope,page);keys=[i['url'] for i in items];dates=[i['listed_date'] for i in items]
                if stream['last'] is not None and last!=stream['last']:raise ValueError('ALRC last page changed')
                if set(keys).intersection(stream['seen']):raise UnsupportedSource('alrc_cross_page_duplicate_identity')
                stream['date_up']|=any(b>a for a,b in zip(dates,dates[1:]))
                if stream['last_date'] is not None and dates[0]>stream['last_date']:stream['date_up']=True
                if incremental and stream['date_up']:raise UnsupportedSource('alrc_listing_order_changed')
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
                if state['emitted'][key]!=item['listed_date']:raise UnsupportedSource('alrc_overlap_date_conflict')
                continue
            state['emitted'][key]=item['listed_date'];output.append(Entry(key,item,item['listed_date']))
        self.can_read_newest=not any(s['date_up'] for s in state['streams'])
        exhausted=all(s['done'] and not s['buffer'] for s in state['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in state['streams'])
        return Page(output,state,exhausted,'both original catalogues plus adjacent empty responses' if exhausted else '',boundary_safe=safe if incremental else True)

    def materialize(self,item):
        c=self.crawler;native=c._alrc_fetch
        def fetch(url):
            if url!=item['url']:raise ValueError('ALRC materializer requested another URL')
            return self.response(url,detail=True).text
        c._alrc_fetch=fetch
        try:paper=c._alrc_paper(item,item['_scope'],item['_list_url'])
        finally:c._alrc_fetch=native
        if paper is None:raise ValueError('ALRC native source body gap; pending retained')
        if paper['url']!=item['url'] or not paper['external_id']:raise UnsupportedSource('alrc_native_detail_identity_unavailable')
        self.stored(item['url'],paper['external_id'])
        return paper
