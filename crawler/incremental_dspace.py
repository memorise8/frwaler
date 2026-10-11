"""Chronological merge of the five delivered Nemertes DSpace browse scopes.

Uses the original title/items membership endpoint with its advertised
'dateaccessioned' sort option. The discovery search endpoint is deliberately
not substituted: its observed membership differs from the delivered browse.
"""
from __future__ import annotations
import copy
import hashlib
import re
from pathlib import Path

from .incremental import Entry, Page, UnsupportedSource, _date, encoded
from .incremental_adapters import Adapter

NEMERTES_SCOPES = (
    'aa75d318-67b9-41ef-b89c-d33db195a08e',
    '38e1d29e-3f53-4755-868e-a36b75554f81',
    '9e4177d9-b92b-4778-a84a-e27f482addb4',
    'f8d85de1-7de0-4348-bca3-10784f86ae71',
    'd533961b-ecdb-497c-b850-8f8dd0594939',
)


def values(meta, key):
    vals = meta.get(key) or []
    if not isinstance(vals, list):
        raise ValueError('invalid DSpace metadata values')
    return [' '.join(str(v.get('value') if isinstance(v,dict) else v).split())
            for v in vals if (v.get('value') if isinstance(v,dict) else v) is not None]


def first(meta, key):
    return next(iter(values(meta,key)), None)


def stamp(item):
    raw = first(item.get('metadata') or {}, 'dc.date.accessioned')
    value = _date(raw)
    if value is None:
        raise UnsupportedSource('dspace_accession_date_unavailable')
    return value


class NemertesAdapter(Adapter):
    rows = 20

    def __init__(self, crawler, *, scopes=None):
        requested = tuple(scopes) if scopes is not None else NEMERTES_SCOPES
        if not requested or len(set(requested)) != len(requested) or not set(requested) <= set(NEMERTES_SCOPES):
            raise ValueError('scope outside delivered Nemertes browse contract')
        super().__init__(crawler, {'base_url':crawler.base_url,'scopes':requested,
                                  'endpoint':'/server/api/discover/browses/title/items',
                                  'sort':'dateaccessioned,desc','rows':self.rows})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()).hexdigest()

    def read(self, scope, page):
        data=self.get(self.scope['base_url']+self.scope['endpoint'],
                      {'scope':scope,'page':page,'size':self.rows,'sort':self.scope['sort']})
        info=data.get('page',{})
        total,pages=info.get('totalElements'),info.get('totalPages')
        items=data.get('_embedded',{}).get('items',[] if total==0 else None)
        if (type(total) is not int or total<0 or type(pages) is not int or pages<0
                or info.get('number')!=page or info.get('size')!=self.rows
                or pages!=(total+self.rows-1)//self.rows or not isinstance(items,list)
                or len(items)!=min(self.rows,max(0,total-page*self.rows))):
            raise ValueError('invalid DSpace page/count contract')
        ids=[];times=[]
        for item in items:
            uid=item.get('uuid')
            if (not isinstance(uid,str) or not re.fullmatch('[0-9a-fA-F-]{36}',uid)
                    or item.get('type')!='item' or item.get('withdrawn') is not False
                    or item.get('inArchive') is not True or item.get('discoverable') is not True):
                raise ValueError('invalid DSpace identity or visibility')
            ids.append(uid);times.append(stamp(item))
        if len(ids)!=len(set(ids)):
            raise ValueError('duplicate DSpace list identities')
        if times!=sorted(times,reverse=True):
            raise UnsupportedSource('dspace_browse_accession_sort_not_honored')
        return items,total,pages,hashlib.sha256(encoded(items).encode()).hexdigest()

    def fetch(self, cursor, direction):
        if cursor is None:
            cursor={'streams':[{'scope':scope,'page':0,'buffer':[],'done':False,
                                'total':None,'anchor':None,'previous_ids':[],'last_date':None}
                               for scope in self.scope['scopes']]}
        else:
            cursor=copy.deepcopy(cursor)
        if [s.get('scope') for s in cursor.get('streams',[])]!=list(self.scope['scopes']):
            raise ValueError('DSpace cursor scope changed')
        output=[];emitted=set()
        while len(output)<self.rows:
            # Before selecting a head, every unfinished scope contributes one.
            # Never stop on known records in scope A before inspecting scope B.
            for stream in cursor['streams']:
                if stream['buffer'] or stream['done']:
                    continue
                page=stream['page']
                if stream['anchor'] is not None:
                    _,total,_,fingerprint=self.read(stream['scope'],page-1)
                    if total!=stream['total'] or fingerprint!=stream['anchor']:
                        raise ValueError('DSpace previous page changed; reconcile required')
                items,total,pages,fingerprint=self.read(stream['scope'],page)
                if stream['total'] is not None and total!=stream['total']:
                    raise ValueError('DSpace scope total changed')
                ids=[d['uuid'] for d in items]
                if set(ids).intersection(stream['previous_ids']):
                    raise ValueError('DSpace repeated source page')
                if items and stream['last_date'] is not None and stamp(items[0])>stream['last_date']:
                    raise UnsupportedSource('dspace_browse_cross_page_order_changed')
                stream.update(buffer=items,total=total,page=page+1,anchor=fingerprint,
                              previous_ids=ids,done=(total==0 or page+1==pages),
                              last_date=stamp(items[-1]) if items else stream['last_date'])
            candidates=[s for s in cursor['streams'] if s['buffer']]
            if not candidates:
                break
            selected=max(candidates,key=lambda s:(stamp(s['buffer'][0]),s['buffer'][0]['uuid']))
            item=selected['buffer'].pop(0)
            if item['uuid'] not in emitted:
                output.append(Entry(item['uuid'],item,first(item['metadata'],'dc.date.accessioned')))
                emitted.add(item['uuid'])
        exhausted=all(s['done'] and not s['buffer'] for s in cursor['streams'])
        return Page(output,cursor,exhausted,'all delivered browse scope pages exhausted' if exhausted else '')

    def materialize(self,item):
        c=self.crawler;uid=item['uuid'];meta=item.get('metadata') or {}
        self.detail_requests+=1
        bundles=self.get(c.base_url+f'/server/api/core/items/{uid}/bundles',{'embed':'bitstreams'})
        info=bundles.get('page',{})
        if ('_embedded' not in bundles and info.get('totalElements')!=0) or info.get('totalPages',1)>1:
            raise ValueError('DSpace bundle pagination requires review')
        for bundle in bundles.get('_embedded',{}).get('bundles',[]):
            bits=bundle.get('_embedded',{}).get('bitstreams',{})
            if bundle.get('name')=='ORIGINAL' and bits.get('page',{}).get('totalPages',1)>1:
                raise ValueError('DSpace original bitstream pagination requires review')
        pdf,filename=c._extract_pdf(bundles)
        handle=item.get('handle');url=f'https://hdl.handle.net/{handle}' if handle else f'{c.base_url}/items/{uid}'
        journal=(first(meta,'dc.relation.ispartof') or first(meta,'oaire.citation.title')
                 or first(meta,'oaire.citationTitle') or first(meta,'dc.relation.ispartofseries') or first(meta,'dc.source'))
        return {'site_id':c.site_id,'external_id':uid,'url':url,
                'title':first(meta,'dc.title') or item.get('name') or '(untitled)',
                'abstract':first(meta,'dc.description.abstract') or first(meta,'dc.description.translatedabstract') or '',
                'published_date':first(meta,'dc.date.issued'),'posted_date':first(meta,'dc.date.accessioned'),
                'authors':values(meta,'dc.contributor.author') or None,'publisher':values(meta,'dc.publisher') or None,
                'keywords':list(dict.fromkeys(values(meta,'dc.subject')+values(meta,'dc.subject.alternative'))) or None,
                'journal':journal,'doi':first(meta,'dc.identifier.doi'),'category':first(meta,'dc.type'),
                'pdf_url':pdf,'original_filename':filename,'metadata':encoded({'raw_metadata':meta,'item_uuid':uid,'handle':handle})}
