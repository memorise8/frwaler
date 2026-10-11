"""Nine delivered archives, merged by observed dates, including research metadata."""
import copy
import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qsl,urlencode,urlsplit,urlunsplit

from bs4 import BeautifulSoup
from .incremental import Entry,Page,Store,UnsupportedSource,_date,encoded
from .incremental_adapters import Adapter

SCOPES=json.loads(Path(__file__).with_name('bankofcanada_incremental_scopes.json').read_text())


class BankOfCanadaAdapter(Adapter):
    rows=10

    def __init__(self,crawler,*,scopes=None):
        selected=list(scopes) if scopes is not None else [s['scope'] for s in SCOPES]
        by_id={s['scope']:s for s in SCOPES}
        if not selected or len(selected)!=len(set(selected)) or not set(selected)<=set(by_id):
            raise ValueError('unknown Bank of Canada delivered scope')
        super().__init__(crawler,{'scopes':[by_id[s] for s in selected],'merge_rows':self.rows})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()).hexdigest()

    def html(self,url,*,detail=False):
        self.http_requests+=1
        if detail:self.detail_requests+=1
        response=self.crawler._request(url,retries=1)
        if response is None:raise ValueError('Bank of Canada fetch failed')
        return BeautifulSoup(response.text,'lxml')

    def detail_paper(self,record):
        if urlsplit(record['url']).netloc!='www.bankofcanada.ca':
            raise UnsupportedSource('bankofcanada_external_detail_identity_date_contract_unverified')
        c=self.crawler;soup=self.html(record['url'],detail=True)
        if c._post_id_from_detail(soup)!=record['post_id']:
            raise ValueError('Bank of Canada detail post identity mismatch')
        canonical=c._canonical_url(soup)
        if canonical and canonical.rstrip('/')!=record['url'].rstrip('/'):
            raise ValueError('Bank of Canada detail canonical mismatch')
        raw=c._meta_content(soup,'publication_date')
        day=c._normalize_date(raw)
        if not day or _date(day) is None:
            raise UnsupportedSource('bankofcanada_precise_detail_publication_date_unavailable')
        parsed=c._parse_detail(soup,record)
        # Never synthesize a first-of-month date from the URL or attach the
        # legacy consumer-survey keywords to the other eight delivered scopes.
        parsed.update(published_date=raw,authors=record.get('authors') or None,
                      keywords=record['tags'],category=record.get('category') or None)
        return {**parsed,'site_id':c.site_id,'publisher':'Bank of Canada','metadata':encoded(parsed['metadata'])},day

    def known_date(self,record):
        db=Store(self.crawler._conn,self.crawler.site_id)
        if db.state().get('scan')!='incremental' or not db.known(record['post_id']):return None
        row=db.row(db.execute('SELECT published_date FROM documents WHERE site_id=? AND post_number=? AND meta_url=?',
                             (self.crawler.site_id,record['post_id'],record['url'])))
        value=str(row['published_date']) if row and row['published_date'] is not None else ''
        return value if _date(value) is not None else None

    def read(self,scope,page,*,resolve_dates=True):
        c=self.crawler;url=urlsplit(scope['archive_url']);pairs=parse_qsl(url.query,keep_blank_values=True)
        target=urlunsplit((url.scheme,url.netloc,url.path,urlencode(pairs+[('mt_page',page)]),''))
        soup=self.html(target);widget=soup.find(id=scope['widget_id'])
        if widget is None or c._page_id(soup)!=scope['source_page_id']:
            raise ValueError('Bank of Canada archive identity missing or changed')
        expected=[v for k,v in pairs if k.startswith('content_type')]
        actual=[n.get('value') for n in widget.select('input[name^="content_type"]')]
        if actual!=expected:raise ValueError('Bank of Canada archive filter mismatch')
        selected=widget.select_one('.pagination .current');position=widget.select_one('input[type="number"][max][min="1"]')
        if selected is None or selected.get_text(strip=True)!=str(page) or position is None or position.get('value')!=str(page):
            raise ValueError('Bank of Canada explicit page position missing')
        last=int(position['max'])
        if not 1<=page<=last:raise ValueError('Bank of Canada invalid terminal page')
        api={'ajax_url':scope['archive_url'],'widgetid':scope['widget_id'],'pageid':scope['source_page_id']}
        records=c._parse_list_records(widget,api,page);nodes=widget.select('article.media')
        if len(records)!=len(nodes) or not 1<=len(records)<=scope['rows_per_page']:
            raise ValueError('Bank of Canada missing/invalid archive records')
        if page<last and len(records)!=scope['rows_per_page']:raise ValueError('Bank of Canada premature short page')
        count=widget.select_one('.num-results');total=int(count.get_text(strip=True).replace(',','')) if count else None
        if total is not None and ((total+scope['rows_per_page']-1)//scope['rows_per_page']!=last
                or len(records)!=min(scope['rows_per_page'],total-(page-1)*scope['rows_per_page'])):
            raise ValueError('Bank of Canada source count mismatch')
        for record,node in zip(records,nodes):
            link=node.select_one('.media-heading a[href]')
            if not record['post_id'].isdigit():
                raise UnsupportedSource('bankofcanada_unstable_list_identity')
            if urlsplit(record['url']).scheme not in ('http','https'):
                raise ValueError('Bank of Canada invalid detail URL')
            record['authors']=[a.get_text(' ',strip=True) for a in node.select('.media-authors a')]
            record['category']=link.get('data-content-type') if link else None
        if len({r['post_id'] for r in records})!=len(records):raise ValueError('Bank of Canada duplicate page IDs')
        fingerprint=hashlib.sha256(encoded(records).encode()).hexdigest()
        if resolve_dates:
            for record in records:
                day=record['published_date'] or self.known_date(record)
                if not day:
                    paper,day=self.detail_paper(record);record['_paper']=paper
                record['_order_date']=day
            dates=[_date(r['_order_date']) for r in records]
            if None in dates or dates!=sorted(dates,reverse=True):
                raise UnsupportedSource('bankofcanada_archive_date_order_not_verified')
        return records,last,total,fingerprint

    def fetch(self,cursor,direction):
        definitions={s['scope']:s for s in self.scope['scopes']}
        if cursor is None:
            cursor={'streams':[{'scope':s,'page':1,'buffer':[],'done':False,'last':None,'total':None,
                                'anchor':None,'ids':[],'last_date':None,'known_pages':0,'unknown_ids':[]} for s in definitions]}
        else:cursor=copy.deepcopy(cursor)
        if [s['scope'] for s in cursor['streams']]!=list(definitions):raise ValueError('Bank of Canada cursor scope mismatch')
        incremental=direction=='newest'
        entries=[];emitted=set()
        while len(entries)<self.rows:
            for stream in cursor['streams']:
                if stream['done'] or (stream['buffer'] and (not incremental or stream['page']>2)):continue
                scope=definitions[stream['scope']];page=stream['page']
                if stream['anchor'] is not None:
                    _,last,total,anchor=self.read(scope,page-1,resolve_dates=False)
                    if (last,total,anchor)!=(stream['last'],stream['total'],stream['anchor']):
                        raise ValueError('Bank of Canada previous page changed; reconcile required')
                records,last,total,anchor=self.read(scope,page)
                if stream['last'] is not None and (last,total)!=(stream['last'],stream['total']):
                    raise ValueError('Bank of Canada source boundary changed')
                if set(r['post_id'] for r in records).intersection(stream['ids']):
                    raise ValueError('Bank of Canada adjacent pages overlap')
                if stream['last_date'] is not None and _date(records[0]['_order_date'])>stream['last_date']:
                    raise UnsupportedSource('bankofcanada_cross_page_order_changed')
                if incremental:
                    db=Store(self.crawler._conn,self.crawler.site_id)
                    unknown=[r['post_id'] for r in records if not db.known(r['post_id']) or not self.exists(Entry(r['post_id'],r))]
                    stream['unknown_ids'].extend(unknown)
                    stream['known_pages']=stream['known_pages']+1 if records and not unknown else 0
                stream.update(buffer=stream['buffer']+records,page=page+1,done=page==last,last=last,total=total,anchor=anchor,
                              ids=[r['post_id'] for r in records],last_date=_date(records[-1]['_order_date']))
            candidates=[s for s in cursor['streams'] if s['buffer']]
            if not candidates:break
            stream=max(candidates,key=lambda s:(_date(s['buffer'][0]['_order_date']),s['buffer'][0]['post_id']))
            record=stream['buffer'].pop(0);key=record['post_id']
            if key in stream['unknown_ids']:stream['unknown_ids'].remove(key)
            if key not in emitted:
                entries.append(Entry(key,record,record['_order_date']));emitted.add(key)
        exhausted=all(s['done'] and not s['buffer'] for s in cursor['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in cursor['streams'])
        return Page(entries,cursor,exhausted,'all delivered archive terminal pages consumed' if exhausted else '',
                    boundary_safe=safe if incremental else True)

    def materialize(self,record):
        return record.get('_paper') or self.detail_paper(record)[0]
