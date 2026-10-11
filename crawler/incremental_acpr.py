"""Original ACPR studies/press pagers with precise card-date merge cursors."""
import copy
import hashlib
from pathlib import Path

from .document_dates import normalize_date
from .incremental import Entry,Page,Store,UnsupportedSource
from .incremental_adapters import Adapter


def day(item):
    value=normalize_date(item['listed_date_raw'],order='DMY')
    if value['status']!='normalized' or value['precision']!='day':
        raise UnsupportedSource('acpr_precise_card_date_unavailable')
    return value['value']


class ACPRAdapter(Adapter):
    rows=12

    def __init__(self,crawler,*,scopes=None,years=None):
        self.native=crawler.crawl.__globals__;selected=list(scopes) if scopes is not None else list(self.native['_SCOPES'])
        if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(self.native['_SCOPES']):raise ValueError('unknown ACPR delivered scope')
        query=None
        if years is not None:
            if selected!=['studies'] or len(years)!=2 or any(type(y) is not int or not 1900<=y<=9999 for y in years) or years[0]>years[1]:
                raise ValueError('invalid ACPR study year bounds')
            query=dict(self.native['_STUDY_QUERY'],**{'start-date_year':str(years[0]),'start-date_month':'01','end-date_year':str(years[1]),'end-date_month':'12'})
        super().__init__(crawler,{'scopes':selected,'years':years,'study_query':query,'rows':self.rows})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
                    +Path(crawler.crawl.__code__.co_filename).read_bytes()
                    +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()

    def html(self,url,*,detail=False):
        self.http_requests+=1
        if detail:self.detail_requests+=1
        res=self.crawler._request(url,retries=1)
        if res is None:raise ValueError('ACPR source fetch failed')
        return res.text

    def read(self,scope,page):
        query=self.scope['study_query'] if scope=='studies' else None
        data=self.native['_listing'](self.html(self.native['_list_url'](scope,page,query)),scope,page,query)
        items=data['items'];dates=[day(x) for x in items]
        if dates!=sorted(dates,reverse=True):raise UnsupportedSource('acpr_card_date_order_changed')
        if self.scope['years'] and any(not self.scope['years'][0]<=int(d[:4])<=self.scope['years'][1] for d in dates):
            raise ValueError('ACPR source ignored requested date bounds')
        if len({x['slug'] for x in items})!=len(items):raise UnsupportedSource('acpr_ambiguous_source_slugs')
        return data

    def fetch(self,cursor,direction):
        incremental=direction=='newest'
        if cursor is None:
            cursor={'streams':[{'scope':s,'page':0,'buffer':[],'done':False,'last_page':None,'anchor':None,
                                'seen':[],'last_date':None,'known_pages':0,'unknown_ids':[]} for s in self.scope['scopes']],
                    'identities':{}}
        else:cursor=copy.deepcopy(cursor)
        if [s['scope'] for s in cursor['streams']]!=self.scope['scopes']:raise ValueError('ACPR cursor scope mismatch')
        entries=[]
        while len(entries)<self.rows:
            for stream in cursor['streams']:
                if stream['done'] or (stream['buffer'] and (not incremental or stream['page']>=2)):continue
                scope,page=stream['scope'],stream['page']
                if stream['anchor'] is not None:
                    previous=self.read(scope,page-1)
                    if previous['page_hash']!=stream['anchor'] or previous['last_page']!=stream['last_page']:
                        raise ValueError('ACPR previous page changed')
                data=self.read(scope,page);items=data['items'];ids=[x['slug'] for x in items]
                if stream['last_page'] is not None and data['last_page']!=stream['last_page']:raise ValueError('ACPR pager boundary changed')
                if set(ids).intersection(stream['seen']):raise ValueError('ACPR repeated source page')
                if items and stream['last_date'] is not None and day(items[0])>stream['last_date']:
                    raise UnsupportedSource('acpr_cross_page_date_order_changed')
                unknown=[]
                for item in items:
                    key=item['slug']
                    if key in cursor['identities'] and cursor['identities'][key]!=item['url']:
                        raise UnsupportedSource('acpr_cross_scope_slug_collision')
                    cursor['identities'][key]=item['url'];self.crawler._assert_identity(item)
                    if incremental and (not Store(self.crawler._conn,self.crawler.site_id).known(key) or not self.exists(Entry(key,item))):unknown.append(key)
                stream['seen'].extend(ids);stream['unknown_ids'].extend(unknown)
                stream.update(buffer=stream['buffer']+[dict(x,_source_scope=scope) for x in items],page=page+1,
                              done=data['next_url'] is None,last_page=data['last_page'],anchor=data['page_hash'],
                              last_date=day(items[-1]) if items else stream['last_date'],
                              known_pages=stream['known_pages']+1 if incremental and items and not unknown else 0)
            candidates=[s for s in cursor['streams'] if s['buffer']]
            if not candidates:break
            stream=max(candidates,key=lambda s:(day(s['buffer'][0]),s['buffer'][0]['slug']))
            item=stream['buffer'].pop(0);key=item['slug']
            if key in stream['unknown_ids']:stream['unknown_ids'].remove(key)
            entries.append(Entry(key,item,day(item)))
        exhausted=all(s['done'] and not s['buffer'] for s in cursor['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in cursor['streams'])
        return Page(entries,cursor,exhausted,'both original scope pagers exhausted' if exhausted else '',boundary_safe=safe if incremental else True)

    def materialize(self,item):
        self.crawler._assert_identity(item)
        return self.crawler._parse_detail(self.html(item['url'],detail=True),item)
