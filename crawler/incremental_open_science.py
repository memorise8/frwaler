"""Original Open Science Canada discovery scopes, ordered by accession time."""
import copy
import hashlib
import json
from pathlib import Path
import re

from .incremental import Entry,Page,Store,UnsupportedSource,_date,encoded
from .incremental_adapters import Adapter


def accession(item):
    values=(item.get('metadata') or {}).get('dc.date.accessioned') or []
    raw=values[0].get('value') if len(values)==1 and isinstance(values[0],dict) else None
    if not isinstance(raw,str) or _date(raw) is None:
        raise UnsupportedSource('open_science_precise_accession_date_unavailable')
    return raw


class OpenScienceAdapter(Adapter):
    rows=25

    def __init__(self,crawler,*,scopes=None,query='*',rows=None):
        selected=list(scopes) if scopes is not None else list(crawler.COMMUNITY_IDS)
        if not selected or len(set(selected))!=len(selected) or not set(selected)<=set(crawler.COMMUNITY_IDS):
            raise ValueError('unknown delivered Open Science community')
        if not isinstance(query,str) or not query.strip():raise ValueError('empty discovery query')
        if rows is not None:
            if type(rows) is not int or not 1<=rows<=100:raise ValueError('invalid discovery page size')
            self.rows=rows
        super().__init__(crawler,{'scopes':selected,'endpoint':crawler.LIST_API_URL,'query':query,
                                  'sort':'dc.date.accessioned,DESC','rows':self.rows})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
                     +Path(crawler.crawl.__code__.co_filename).read_bytes()
                     +Path(__file__).with_name('open_science_titles.py').read_bytes()).hexdigest()

    def read(self,scope,page):
        c=self.crawler;data=self.get(self.scope['endpoint'],{'scope':scope,'query':self.scope['query'],
                              'page':page,'size':self.rows,'sort':self.scope['sort']})
        if data.get('scope')!=scope or data.get('query')!=self.scope['query'] or data.get('sort')!={'by':'dc.date.accessioned','order':'DESC'}:
            raise ValueError('Open Science query/sort echo mismatch')
        result=data.get('_embedded',{}).get('searchResult',{});info=result.get('page',{})
        total,pages=info.get('totalElements'),info.get('totalPages');records=result.get('_embedded',{}).get('objects',[])
        if (type(total) is not int or total<0 or type(pages) is not int or pages!=(total+self.rows-1)//self.rows
                or info.get('number')!=page or info.get('size')!=self.rows or not isinstance(records,list)
                or page>max(0,pages-1) or len(records)!=min(self.rows,max(0,total-page*self.rows))):
            raise ValueError('Open Science source count/page mismatch')
        items=[c._indexable_object(r) for r in records];ids=[];dates=[]
        for item in items:
            uid=item.get('uuid');kind=item.get('type')
            if not isinstance(uid,str) or not re.fullmatch('[0-9a-fA-F-]{36}',uid) or kind not in ('item','collection','community'):
                raise UnsupportedSource('open_science_source_identity_unverified')
            ids.append(uid)
            if kind=='item':
                if item.get('withdrawn') is not False or item.get('inArchive') is not True or item.get('discoverable') is not True:
                    raise ValueError('Open Science visibility not verified')
                dates.append(_date(accession(item)))
        if len(ids)!=len(set(ids)):raise ValueError('Open Science repeated list identities')
        if dates!=sorted(dates,reverse=True):raise UnsupportedSource('open_science_accession_sort_not_honored')
        return items,total,pages,hashlib.sha256(encoded(items).encode()).hexdigest()

    def fetch(self,cursor,direction):
        incremental=direction=='newest'
        if cursor is None:
            cursor={'streams':[{'scope':s,'page':0,'buffer':[],'done':False,'total':None,'anchor':None,
                'seen_ids':[],'last_date':None,'known_pages':0,'unknown_ids':[]} for s in self.scope['scopes']]}
        else:cursor=copy.deepcopy(cursor)
        if [s['scope'] for s in cursor['streams']]!=self.scope['scopes']:raise ValueError('Open Science cursor scope changed')
        output=[];emitted=set()
        while len(output)<self.rows:
            waiting=False
            for stream in cursor['streams']:
                if stream['done'] or (stream['buffer'] and (not incremental or stream['page']>=2)):continue
                scope,page=stream['scope'],stream['page']
                if stream['anchor'] is not None:
                    _,total,_,fingerprint=self.read(scope,page-1)
                    if total!=stream['total'] or fingerprint!=stream['anchor']:raise ValueError('Open Science previous page changed')
                items,total,pages,fingerprint=self.read(scope,page);ids=[x['uuid'] for x in items]
                if stream['total'] is not None and total!=stream['total']:raise ValueError('Open Science scope total changed')
                if set(ids).intersection(stream['seen_ids']):raise ValueError('Open Science repeated source page')
                stream['seen_ids'].extend(ids);docs=[x for x in items if x['type']=='item']
                if docs and stream['last_date'] is not None and _date(accession(docs[0]))>stream['last_date']:
                    raise UnsupportedSource('open_science_cross_page_order_changed')
                unknown=[]
                for item in docs:
                    self.crawler._stored_cursor_item(item)
                    if incremental and (not Store(self.crawler._conn,self.crawler.site_id).known(item['uuid']) or not self.exists(Entry(item['uuid'],item))):
                        unknown.append(item['uuid'])
                stream['unknown_ids'].extend(unknown)
                stream.update(buffer=stream['buffer']+[dict(x,_source_scope=scope) for x in docs],page=page+1,
                              total=total,anchor=fingerprint,done=(total==0 or page+1==pages),
                              last_date=_date(accession(docs[-1])) if docs else stream['last_date'],
                              known_pages=stream['known_pages']+1 if incremental and docs and not unknown else 0)
                if stream['done'] and len(stream['seen_ids'])!=total:raise ValueError('Open Science terminal unique count mismatch')
                if not stream['buffer'] and not stream['done']:waiting=True
            if waiting:break # structural page: persist progress, obtain every scope's head before merging
            candidates=[s for s in cursor['streams'] if s['buffer']]
            if not candidates:break
            stream=max(candidates,key=lambda s:(_date(accession(s['buffer'][0])),s['buffer'][0]['uuid']))
            item=stream['buffer'].pop(0);key=item['uuid']
            if key in stream['unknown_ids']:stream['unknown_ids'].remove(key)
            if key not in emitted:output.append(Entry(key,item,accession(item)));emitted.add(key)
        exhausted=all(s['done'] and not s['buffer'] for s in cursor['streams'])
        safe=all((s['done'] or s['known_pages']>=2) and not s['unknown_ids'] for s in cursor['streams'])
        evidence='all original community entity totals and final pages observed' if exhausted else 'structural entity page checkpoint'
        return Page(output,cursor,exhausted,evidence,checkpoint_only=not output and not exhausted,
                    boundary_safe=safe if incremental else True)

    def json_detail(self,url,context='request'):
        self.detail_requests+=1
        return self.get(url)

    def text_detail(self,url,context='request',accept=None):
        self.http_requests+=1;self.detail_requests+=1
        response=self.crawler._request(url,retries=1,headers={'Accept':accept or 'text/html'})
        if response is None:raise ValueError('Open Science body fetch failed')
        return response.text

    def materialize(self,item):
        c=self.crawler;uid=item['uuid'];c._active_community=item['_source_scope'];c._stored_cursor_item(item)
        old_json,old_get=c._curl_json,c._curl_get
        c._curl_json,c._curl_get=self.json_detail,self.text_detail
        try:
            detail=item if c._complete_inline_item(item,uid) else c._fetch_item(uid)
            if not detail or detail.get('uuid')!=uid:raise ValueError('Open Science detail identity mismatch')
            bits=c._fetch_bitstreams(detail,uid);parsed=c._parse_item(detail,bits)
            if getattr(c,'_access_held',False) or len(parsed['abstract'])<c.MIN_ABSTRACT_CHARS:
                raise ValueError('Open Science source body unavailable; pending retained')
            # Match the native _save_parsed mapping, including original date precision.
            paper={k:parsed[k] for k in ('external_id','title','abstract','category','published_date','listed_date','url','pdf_url','doi','department')}
            paper.update(site_id=c.site_id,authors=json.dumps(parsed['authors'],ensure_ascii=False),
                         keywords=json.dumps(parsed['keywords'],ensure_ascii=False),metadata=json.dumps(parsed['metadata'],ensure_ascii=False))
            return paper
        finally:c._curl_json,c._curl_get=old_json,old_get
