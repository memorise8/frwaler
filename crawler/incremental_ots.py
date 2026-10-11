"""OTS emitter-54 listing with validated counts, anchors and source timestamps."""
from datetime import datetime,timezone
import hashlib
from pathlib import Path
import re

from .incremental import Entry,Page,Store,UnsupportedSource,encoded
from .incremental_adapters import Adapter


def number(value):
    if type(value) not in (str,int) or not re.fullmatch(r'0|[1-9]\d*',str(value)):
        raise ValueError('invalid OTS numeric metadata')
    return int(value)


class OTSAdapter(Adapter):
    rows=10
    endpoint='https://core2023.ots.at/api/v1'

    def __init__(self,crawler,*,query=''):
        if not isinstance(query,str):raise ValueError('invalid OTS query')
        super().__init__(crawler,{'endpoint':self.endpoint,'emittentId':'54','query':query,'rows':self.rows})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()).hexdigest()

    def identity(self,item):
        key=item.get('key')
        if not isinstance(key,str) or not re.fullmatch(r'OTS_\d{8}_OTS\d+',key):
            raise UnsupportedSource('ots_unrecognized_stable_identity')
        if str(item.get('emittentId'))!=self.scope['emittentId']:
            raise ValueError('OTS emitter scope mismatch')
        if self.crawler._iso_date(item.get('date'))!=f'{key[4:8]}-{key[8:10]}-{key[10:12]}':
            raise ValueError('OTS identity/date mismatch')
        return key

    def read(self,page):
        data=self.get(self.endpoint+'/search',{'query':self.scope['query'],'page':page,
                                             'maxCountPerPage':self.rows,'emittentId':self.scope['emittentId']})
        if data.get('error')!=[]:raise ValueError('OTS search error envelope')
        meta=data.get('meta',{});items=data.get('result')
        total=number(meta.get('totalCount'));pages=number(meta.get('totalPages'))
        if (not isinstance(items,list) or number(meta.get('count'))!=len(items)
            or number(meta.get('page'))!=page or meta.get('endpoint')!='search'
            or str(meta.get('emittentId'))!=self.scope['emittentId']
            or pages!=(total+self.rows-1)//self.rows
            or number(meta.get('maxCountPerPage')) not in ([self.rows,0] if total==0 else [self.rows])
            or len(items)!=min(self.rows,max(0,total-(page-1)*self.rows))):
            raise ValueError('invalid OTS page/count/scope contract')
        entries=[]
        for item in items:
            key=self.identity(item)
            try:stamp=datetime.fromtimestamp(number(item.get('timestamp')),timezone.utc).isoformat()
            except (ValueError,OverflowError,OSError) as exc:
                raise UnsupportedSource('ots_posting_timestamp_unavailable') from exc
            entries.append(Entry(key,item,stamp))
        if len({e.key for e in entries})!=len(entries):raise ValueError('duplicate OTS page identities')
        return entries,total,pages,hashlib.sha256(encoded(items).encode()).hexdigest()

    def fetch(self,cursor,direction):
        if cursor is None:
            first,total,pages,fingerprint=self.read(1)
            step=-1 if direction=='newest' and Store(self.crawler._conn,self.crawler.site_id).state().get('order')=='oldest_first' else 1
            page=max(1,pages) if step==-1 else 1
            entries=first
            if page!=1:
                entries,other,other_pages,fingerprint=self.read(page)
                if (other,other_pages)!=(total,pages):raise ValueError('OTS source total changed')
        else:
            page,step=cursor['page'],cursor['step']
            _,total,pages,fingerprint=self.read(cursor['anchor_page'])
            if (total,pages,fingerprint)!=(cursor['total'],cursor['pages'],cursor['anchor']):
                raise ValueError('OTS previous page changed; reconcile required')
            entries,other,other_pages,fingerprint=self.read(page)
            if (other,other_pages)!=(total,pages):raise ValueError('OTS source total changed')
            if set(e.key for e in entries).intersection(cursor['ids']):raise ValueError('OTS adjacent source pages overlap')
        if step==-1:entries=list(reversed(entries))
        exhausted=total==0 or (page==pages if step==1 else page==1)
        return Page(entries,{'page':page+step,'step':step,'anchor_page':page,'anchor':fingerprint,
                             'total':total,'pages':pages,'ids':[e.key for e in entries]},exhausted,
                    'OTS validated totalCount and terminal page membership' if exhausted else '')

    def materialize(self,item):
        c=self.crawler;key=self.identity(item);self.detail_requests+=1
        data=self.get(self.endpoint+'/pressrelease/'+key)
        detail=data.get('result')
        if data.get('error')!=[] or not isinstance(detail,dict) or self.identity(detail)!=key:
            raise ValueError('OTS detail identity mismatch or error')
        title=(detail.get('title') or {}).get('content') or (item.get('title') or {}).get('content')
        article=detail.get('article')
        if isinstance(article,dict):
            # Older releases encode sparse ordered paragraphs as a JSON object.
            # Numeric order matters: lexical order would put paragraph 10 before 2.
            article=[article[k] for k in sorted(article,key=number)]
        if not title or not isinstance(article,list) or any(not isinstance(p,dict) for p in article):
            raise ValueError('invalid OTS article envelope')
        lead=c._normalize_ws((detail.get('lead') or {}).get('content'))
        pdf=c._extract_pdf_url(detail);slug=detail.get('titleSlug') or item.get('titleSlug') or ''
        return {'site_id':c.site_id,'external_id':key,'title':title,
                'url':c.base_url+'/presseaussendung/'+key+'/'+slug,
                'abstract':c._build_abstract_from_json(dict(detail,article=article),lead),
                'published_date':detail.get('date'),'posted_date':str(item.get('date',''))+' '+str(item.get('time','')),
                'publisher':detail.get('emittentName') or item.get('emittentName'),
                'keywords':detail.get('keywords') or None,
                'pdf_url':pdf,'original_filename':pdf.rstrip('/').rsplit('/',1)[-1] if pdf else None,
                'category':', '.join(x['name'] for x in detail.get('channel',[]) if isinstance(x,dict) and x.get('name')) or None,
                'metadata':encoded({'ots_id':key,'aussender_id':self.scope['emittentId'],'source_detail':detail})}
