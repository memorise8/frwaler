"""AFP news native scope with observed Drupal pager and filter-only end shape."""
from datetime import date,datetime
import hashlib
from pathlib import Path
from urllib.parse import parse_qs,urlencode,urlsplit

from .incremental import Entry,Page,UnsupportedSource,encoded
from .incremental_adapters import Adapter

BASE='https://www.afp.gov.au'
ENDPOINT=BASE+'/news-centre'


class AFPNewsAdapter(Adapter):
    def __init__(self,crawler,*,date_from=None,date_to=None):
        if bool(date_from)!=bool(date_to):raise ValueError('both AFP date bounds are required')
        if date_from and date.fromisoformat(date_from)>date.fromisoformat(date_to):
            raise ValueError('reversed AFP date bounds')
        super().__init__(crawler,dict(endpoint=ENDPOINT,date_from=date_from or '',date_to=date_to or ''))
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()).hexdigest()

    def url(self,page):
        q={k:self.scope[k] for k in ('date_from','date_to') if self.scope[k]}
        if page:q['page']=str(page)
        return ENDPOINT+('?' + urlencode(q) if q else '')

    def response(self,url,*,detail=False,method='GET'):
        self.http_requests+=1;self.detail_requests+=int(detail)
        r=self.crawler._session.request(method,url,timeout=35,allow_redirects=False,
                                        headers={'Accept-Encoding':'gzip, deflate'})
        r.raise_for_status()
        if r.is_redirect:raise ValueError('AFP redirect requires scope review')
        return r

    def next_page(self,url,page):
        p=urlsplit(url);q=parse_qs(p.query,keep_blank_values=True)
        expected={k:[self.scope[k]] for k in ('date_from','date_to') if self.scope[k]}
        expected['page']=[str(page+1)]
        for k,v in [('field_article_type_target_id','All'),('title',''),('sort_by','created')]:
            if k in q:
                if q.pop(k)!=[v]:raise ValueError('AFP next link changed filter')
        if p.scheme!='https' or p.netloc!='www.afp.gov.au' or p.path!='/news-centre' or p.fragment or q!=expected:
            raise ValueError('AFP next link changed page or scope')

    def read(self,page,*,allow_empty=False):
        c=self.crawler;url=self.url(page);raw=self.response(url).text
        soup=c.crawl.__globals__['_make_soup'](raw,context='incremental list')
        if soup is None or '</html>' not in raw.lower() or [h.get_text(' ',strip=True) for h in soup.select('h1')]!=['News Centre']:
            raise ValueError('AFP original news document missing')
        views=soup.select('.view-solr-news-centre.view-display-id-all_news_page')
        if len(views)!=1:raise ValueError('AFP news view missing or ambiguous')
        view=views[0];forms=view.select('form[action="/news-centre"]')
        if len(forms)!=1:raise ValueError('AFP filter form missing')
        form=forms[0]
        for key,value in [('date_from',self.scope['date_from']),('date_to',self.scope['date_to']),('title','')]:
            fields=form.select(f'input[name="{key}"]')
            if len(fields)!=1 or fields[0].get('value','')!=value:raise ValueError('AFP filter echo differs')
        checked=form.select('input[name="field_article_type_target_id"][checked]')
        selected=form.select('select[name="sort_by"] option[selected]')
        if len(checked)!=1 or checked[0].get('value')!='All' or len(selected)!=1 or selected[0].get('value')!='created':
            raise ValueError('AFP default all-news/date sort changed')
        items,next_url=c._parse_list_page(raw,url);cards=view.select('.view-content .views-row')
        if len(items)!=len(cards):raise UnsupportedSource('afp_news_native_parser_skipped_card')
        if not items:
            # Observed adjacent final page: exactly the original header and
            # filters, no view-content, message, pager, or partial document.
            children=[set(t.get('class',[])) for t in view.find_all(recursive=False)]
            filter_box=view.select_one('.view-filters')
            clean_filter=filter_box is not None and filter_box.find_all(recursive=False)==[form]
            if (not allow_empty or children!=[{'view-header'},{'view-filters'}] or next_url
                    or not clean_filter or view.select('.messages,.alert,[role="alert"]')):
                raise ValueError('AFP empty response lacks observed source-end shape')
            return [],None
        if not 1<=len(items)<=100:raise ValueError('AFP unexpected list cardinality')
        for item in items:
            p=urlsplit(item['url'])
            if (not item['node_id'].isdecimal() or p.scheme!='https' or p.netloc!='www.afp.gov.au'
                    or not p.path.startswith('/news-centre/') or p.query or p.fragment):
                raise UnsupportedSource('afp_news_unstable_listing_identity')
            try:
                parsed=datetime.fromisoformat(item['listed_datetime'])
                if parsed.tzinfo is None:raise ValueError('missing timezone')
            except (TypeError,ValueError):raise UnsupportedSource('afp_news_precise_listing_datetime_missing')
        if len({i['node_id'] for i in items})!=len(items) or len({i['url'] for i in items})!=len(items):
            raise UnsupportedSource('afp_news_repeated_listing_identity')
        if next_url:self.next_page(next_url,page)
        return items,next_url

    def fetch(self,cursor,direction):
        page=cursor['page'] if cursor else 0
        if cursor:
            prior,_=self.read(page-1)
            if hashlib.sha256(encoded(prior).encode()).hexdigest()!=cursor['anchor']:
                raise ValueError('AFP previous page drift; reconcile required')
        items,nxt=self.read(page);keys=[i['node_id'] for i in items];urls=[i['url'] for i in items]
        if cursor and (set(keys).intersection(cursor['seen']) or set(urls).intersection(cursor['urls'])):
            raise UnsupportedSource('afp_news_cross_page_identity_repeated')
        exhausted=nxt is None
        if exhausted:
            adjacent,_=self.read(page+1,allow_empty=True)
            if adjacent:raise ValueError('AFP absent next link contradicted by adjacent items')
        times=[datetime.fromisoformat(i['listed_datetime']).timestamp() for i in items]
        up=bool((cursor or {}).get('date_up')) or any(b>a for a,b in zip(times,times[1:]))
        if cursor and times[0]>cursor['last_time']:up=True
        self.can_read_newest=not up
        state=dict(page=page+1,anchor=hashlib.sha256(encoded(items).encode()).hexdigest(),
                   seen=(cursor or {}).get('seen',[])+keys,urls=(cursor or {}).get('urls',[])+urls,
                   last_time=times[-1],date_up=up)
        return Page([Entry(i['node_id'],i,i['listed_datetime']) for i in items],state,exhausted,
                    'native absent next link plus adjacent original filter-only view' if exhausted else '')

    def materialize(self,item):
        c=self.crawler;raw=self.response(item['url'],detail=True).text
        native_head=c._curl_head_content_disposition
        c._curl_head_content_disposition=lambda url:self.response(url,method='HEAD').headers.get('Content-Disposition')
        try:detail=c._parse_detail_page(raw,item['url'])
        finally:c._curl_head_content_disposition=native_head
        if detail.get('node_id')!=item['node_id']:
            raise UnsupportedSource('afp_news_detail_identity_differs_from_listing')
        paper=c._build_paper(item,detail)
        if len(paper.get('abstract') or '')<c._MIN_ABSTRACT_CHARS:
            raise ValueError('AFP native detail body unavailable; pending retained')
        if paper['external_id']!=item['node_id'] or paper['url']!=item['url']:
            raise UnsupportedSource('afp_news_native_storage_identity_changed')
        return paper
