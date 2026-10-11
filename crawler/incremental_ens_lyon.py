"""ENS press discovery, preserving native short-body exclusions and stored IDs."""
import copy
import hashlib
from pathlib import Path
from urllib.parse import parse_qs,urljoin,urlsplit

from .document_dates import normalize_date
from .ens_lyon_traversal import digest
from .incremental import Entry,Page,Store,UnsupportedSource
from .incremental_adapters import Adapter


class EnsLyonAdapter(Adapter):
    rows=12

    def __init__(self,crawler):
        super().__init__(crawler,{'endpoint':crawler.LIST_URL,'rows':self.rows,'eligibility':'native_short_body_exclusion'})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('ens_lyon_traversal.py').read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()
        state=Store(crawler._conn,crawler.site_id).state()
        self.prior_exclusions=((state.get('cursor') or {}).get('exclusions',{})
            if state.get('phase')=='idle' and state.get('baseline_complete') and state.get('contract')==self.contract else {})
        self.resume_exclusions=((state.get('cursor') or {}).get('exclusions',{})
            if state.get('phase')=='deliver' and state.get('contract')==self.contract else {})

    def html(self,url,*,detail=False):
        self.http_requests+=1
        if detail:self.detail_requests+=1
        r=self.crawler._request(url,retries=1,headers={'Accept-Encoding':'gzip, deflate'})
        if r is None:raise ConnectionError('ENS source response unavailable')
        return r.text

    def pager_number(self,href):
        p=urlsplit(urljoin(self.scope['endpoint'],href));base=urlsplit(self.scope['endpoint'])
        query=parse_qs(p.query,keep_blank_values=True)
        if ((p.scheme,p.netloc,p.path,p.fragment)!=(base.scheme,base.netloc,base.path,'')
                or set(query)!={'page'} or len(query['page'])!=1 or not query['page'][0].isdecimal()):
            raise ValueError('ENS pager scope changed')
        return int(query['page'][0])

    def read(self,page,*,empty_after=None):
        c=self.crawler;url=self.scope['endpoint']+'?page='+str(page);raw=self.html(url)
        native=c._curl_get
        def retained(target,**kwargs):
            if target!=url:raise ValueError('ENS native list requested another scope')
            return raw
        c._curl_get=retained
        try:data=c._ens_page(page)
        finally:c._curl_get=native
        soup=c._make_soup(raw);view=soup.select_one('.view-communiques-de-presse')
        current=view.select('.pager a[aria-current][href]')
        numbers=[self.pager_number(a['href']) for a in view.select('.pager a[href]')]
        if not numbers or len(current)!=1:raise ValueError('ENS source pager missing')
        last=max(numbers);selected=self.pager_number(current[0]['href'])
        if not data['items']:
            if empty_after is None or last!=empty_after or selected!=empty_after or page!=empty_after+1 or data['has_next']:
                raise ValueError('ENS empty view is not verified adjacent exhaustion')
            return dict(data,last_page=last)
        if (selected!=page or data['has_next']!=(page<last) or not 1<=len(data['items'])<=self.rows
                or data['has_next'] and len(data['items'])!=self.rows):
            raise ValueError('ENS current/last/next pager mismatch')
        cards=view.select('article')
        for item,card in zip(data['items'],cards):
            title=c._node_text(card.select_one('.field--name-title, h3'))
            raw_date=c._node_text(card.select_one('.field--name-field-article-date'))
            value=normalize_date(raw_date.removeprefix('Date de publication').strip(),order='DMY')
            if not title or value['status']!='normalized' or value['precision']!='day':
                raise UnsupportedSource('ens_precise_list_date_or_title_unavailable')
            item.update(title=title,date=value['value'],listed_date_raw=raw_date)
        dates=[i['date'] for i in data['items']]
        if dates!=sorted(dates,reverse=True):raise UnsupportedSource('ens_list_date_order_changed')
        return dict(data,last_page=last)

    def prepare(self,item):
        c=self.crawler;c._ens_row(item,required=False)
        paper=c._parse_article(item['key'],item['url'],self.html(item['url'],detail=True))
        if c.delivery_source_failure:raise ValueError('ENS detail failed; pending discovery retained')
        if paper is None:
            if not c._ens_last_exclusion or c._ens_row(item,required=False) is not None:
                raise ValueError('ENS detail neither stored nor explicitly excluded')
            return None,{'item':item,'fingerprint':c._ens_last_exclusion,'reason':'native_source_body_below_minimum'}
        if paper['title']!=item['title']:raise ValueError('ENS detail/list title mismatch')
        return paper,None

    def fetch(self,cursor,direction):
        incremental=direction=='newest'
        state=copy.deepcopy(cursor) if cursor else dict(page=0,seen={},urls={},
            exclusions=copy.deepcopy(self.prior_exclusions) if incremental else {},memberships=0,duplicates=0)
        page=state['page']
        if cursor:
            previous=self.read(page-1)
            if previous['page_hash']!=state['anchor'] or previous['last_page']!=state['last_page']:
                raise ValueError('ENS previous page changed; reconcile required')
        data=self.read(page)
        if cursor and data['last_page']!=state['last_page']:raise ValueError('ENS last page changed')
        if cursor and data['items'][0]['date']>state['last_date']:
            raise UnsupportedSource('ens_cross_page_date_order_changed')
        entries=[];fresh=0
        for item in data['items']:
            key,url=item['key'],item['url'];identity={k:item[k] for k in ('url','title','date')}
            state['memberships']+=1
            if key in state['seen']:
                if state['seen'][key]!=identity:raise UnsupportedSource('ens_conflicting_duplicate_membership')
                # A repeated page-boundary row can hide another source row.
                # Identical ID/URL/date is not proof of a legitimate duplicate.
                raise ValueError('ENS repeated cross-page membership; snapshot reconciliation required')
            if url in state['urls'] and state['urls'][url]!=key:raise UnsupportedSource('ens_URL_reused_with_different_ID')
            state['seen'][key]=identity;state['urls'][url]=key;fresh+=1
            entry=Entry(key,item,item['date'])
            if not incremental or not Store(self.crawler._conn,self.crawler.site_id).known(key) or not self.exists(entry):
                paper,excluded=self.prepare(item)
                if excluded:
                    state['exclusions'][key]=excluded;continue
                state['exclusions'].pop(key,None)
                item['_prepared_paper']=paper
            entries.append(entry)
        if not fresh:raise ValueError('ENS source page repeats earlier memberships only')
        exhausted=False
        if not data['has_next']:
            empty=self.read(page+1,empty_after=page)
            if empty['items']:raise ValueError('ENS advertised final page contradicted by adjacent records')
            exhausted=True
        state.update(page=page+1,anchor=data['page_hash'],last_page=data['last_page'],last_date=data['items'][-1]['date'])
        return Page(entries,state,exhausted,
                    'final source pager plus adjacent empty view; native exclusions recorded' if exhausted else
                    'source page contains only native exclusions or verified duplicate memberships' if not entries else '',
                    checkpoint_only=not entries and not exhausted)

    def exists(self,entry):
        return self.crawler._ens_row(entry.payload,required=False) is not None

    def materialize(self,item):
        c=self.crawler
        # A resumed delivery rechecks explicit exclusions before approving its
        # saved baseline, as the native backfill cursor already requires.
        for excluded in self.resume_exclusions.values():
            native=c._curl_get;c._curl_get=lambda url,**kwargs:self.html(url,detail=True)
            try:c._ens_excluded_prefix(excluded['item'],excluded['fingerprint'])
            finally:c._curl_get=native
        self.resume_exclusions={}
        c._ens_row(item,required=False)
        if '_prepared_paper' not in item:raise ValueError('ENS uncached delivery item')
        return item['_prepared_paper']
