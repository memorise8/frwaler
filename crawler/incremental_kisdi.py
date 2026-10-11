"""KISDI's three delivered masters, merged without cross-scope early loss."""
import copy
from datetime import date
import hashlib
from pathlib import Path

from .incremental import Entry,Page,Store,UnsupportedSource,encoded
from .incremental_adapters import Adapter


class KisdiAdapter(Adapter):
    rows=10

    def __init__(self,crawler,*,scopes=None):
        self.native=crawler.crawl.__globals__
        selected=list(scopes) if scopes is not None else list(self.native['_SCOPES'])
        if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(self.native['_SCOPES']):
            raise ValueError('unknown KISDI delivered scope')
        super().__init__(crawler,{'scopes':{s:self.native['_SCOPES'][s] for s in selected},'scope_order':selected,'merge_rows':self.rows})
        # Native listing, body/PDF extraction and identity guards are reused.
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
                                    +Path(crawler.crawl.__code__.co_filename).read_bytes()).hexdigest()

    def html(self,url,*,detail=False):
        self.http_requests+=1
        if detail:self.detail_requests+=1
        response=self.crawler._request(url,retries=1,method='POST' if detail else 'GET',
                    **({'data':{'pageIndex':'1'}} if detail else {}))
        if response is None:raise ValueError('KISDI source fetch failed')
        return response.text

    def read(self,scope,page):
        data=self.native['_listing'](self.html(self.native['_list_url'](scope,page)),scope,page)
        dates=[]
        for item in data['items']:
            raw=item['published_date_raw']
            try:
                if len(raw)!=10 or date.fromisoformat(raw).isoformat()!=raw:raise ValueError()
            except ValueError as exc:raise UnsupportedSource('kisdi_precise_list_date_unavailable') from exc
            dates.append(raw)
        if dates!=sorted(dates,reverse=True):raise UnsupportedSource('kisdi_scope_date_order_changed')
        return data

    def known(self,item):
        return bool(Store(self.crawler._conn,self.crawler.site_id).known(item['art_id'])) and self.exists(Entry(item['art_id'],item))

    def fetch(self,cursor,direction):
        initial=cursor is None;incremental=direction=='newest'
        if initial:
            cursor={'streams':[{'scope':s,'page':1,'buffer':[],'done':False,'bounds':None,'anchor':None,
                                'last_date':None,'known_pages':0,'unknown_ids':[]} for s in self.scope['scopes']],
                    'identities':{}}
        else:cursor=copy.deepcopy(cursor)
        if [s['scope'] for s in cursor['streams']]!=list(self.scope['scopes']):raise ValueError('KISDI cursor scope mismatch')

        def advance(stream):
            scope,page=stream['scope'],stream['page']
            if stream['anchor'] is not None:
                old=self.read(scope,page-1)
                if old['page_hash']!=stream['anchor'] or [old[k] for k in ('total','last_page','page_size')]!=stream['bounds']:
                    raise ValueError('KISDI previous page changed; reconcile required')
            data=self.read(scope,page);bounds=[data[k] for k in ('total','last_page','page_size')];items=data['items']
            if stream['bounds'] is not None and bounds!=stream['bounds']:raise ValueError('KISDI source count changed')
            if items and stream['last_date'] is not None and items[0]['published_date_raw']>stream['last_date']:
                raise UnsupportedSource('kisdi_cross_page_date_order_changed')
            all_known=bool(items)
            for item in items:
                uid=item['art_id']
                if uid in cursor['identities']:raise UnsupportedSource('kisdi_repeated_or_cross_master_identity')
                cursor['identities'][uid]=item['master_id']
                self.crawler._assert_identity(item)
                if incremental and not self.known(item):stream['unknown_ids'].append(uid);all_known=False
                stream['buffer'].append(dict(item,_scope=scope))
            stream.update(page=page+1,bounds=bounds,anchor=data['page_hash'],
                          done=(data['total']==0 or page==data['last_page']),
                          last_date=items[-1]['published_date_raw'] if items else stream['last_date'],
                          known_pages=stream['known_pages']+1 if incremental and all_known else 0)

        if initial and incremental:
            # Read each scope's actual first page and one overlap page before
            # allowing a merged known boundary. An older journal may have new
            # records below a different scope's already-known dates.
            for stream in cursor['streams']:
                for _ in range(2):
                    if not stream['done']:advance(stream)
        output=[]
        while len(output)<self.rows:
            for stream in cursor['streams']:
                if not stream['buffer'] and not stream['done']:advance(stream)
            candidates=[s for s in cursor['streams'] if s['buffer']]
            if not candidates:break
            stream=max(candidates,key=lambda s:(s['buffer'][0]['published_date_raw'],s['buffer'][0]['art_id']))
            item=stream['buffer'].pop(0);uid=item['art_id']
            if uid in stream['unknown_ids']:stream['unknown_ids'].remove(uid)
            output.append(Entry(uid,item,item['published_date_raw']))
        exhausted=all(s['done'] and not s['buffer'] for s in cursor['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in cursor['streams'])
        return Page(output,cursor,exhausted,'all delivered master list totals and final pages observed' if exhausted else '',
                    boundary_safe=safe if incremental else True)

    def materialize(self,item):
        c=self.crawler;scope=item['_scope']
        if item['url']!=self.native['_detail_url'](scope,item['art_id']):raise ValueError('KISDI pending identity mismatch')
        c._assert_identity(item)
        # Keep native HTML-summary/PDF-body guards. Failed extraction retains
        # this pending record and cannot certify a complete baseline.
        return c._parse_detail(self.html(item['url'],detail=True),item)
