"""Native statistical-series scope; document pages are not individual data releases.

Periodic full checks remain necessary for attachments and content changing at
stable series URLs. Other delivered APRA scopes are not covered by this adapter.
"""
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs,urljoin,urlsplit

from .incremental import Entry,Page,Store,UnsupportedSource,encoded
from .incremental_adapters import Adapter
from delivery.health.access_challenge import access_challenge


class APRAAdapter(Adapter):
    def __init__(self,crawler):
        super().__init__(crawler,dict(endpoint=crawler.crawl.__globals__['_LIST_URL'],unit='native_statistical_series_page'))
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()

    def url(self,page):
        return self.scope['endpoint']+('&page='+str(page) if page else '')

    def response(self,url,*,detail=False):
        self.http_requests+=1;self.detail_requests+=int(detail)
        r=self.crawler._session.get(url,timeout=35,allow_redirects=False,headers={'Accept-Encoding':'gzip, deflate'})
        r.raise_for_status()
        if r.is_redirect:raise ValueError('APRA redirect requires scope review')
        if access_challenge(r.text):raise ValueError('APRA access challenge is not source exhaustion')
        return r.text

    def page_number(self,href):
        p=urlsplit(urljoin(self.scope['endpoint'],href));base=urlsplit(self.scope['endpoint']);q=parse_qs(p.query)
        n=q.pop('page',None)
        if ((p.scheme,p.netloc,p.path,p.fragment)!=(base.scheme,base.netloc,base.path,'')
                or q!=parse_qs(base.query) or not n or len(n)!=1 or not n[0].isdecimal()):
            raise ValueError('APRA pager changed scope')
        return int(n[0])

    def read(self,page,*,empty_after=None):
        raw=self.response(self.url(page));g=self.crawler.crawl.__globals__;soup=g['_bs'](raw)
        if soup is None or '</html>' not in raw.lower() or [h.get_text(' ',strip=True) for h in soup.select('h1')]!=['News and Publications']:
            raise ValueError('APRA incomplete or unexpected listing')
        forms=soup.select('form#views-exposed-form-news-and-publications-page-1')
        if len(forms)!=1:raise ValueError('APRA filter form missing')
        form=forms[0]
        for key,values in [('document_type[]',['bundle:statistical_publication']),('created',['All']),
                           ('sort_bef_combine',['created_DESC']),('field_industry_target_id[]',[])]:
            select=form.find('select',attrs={'name':key})
            if select is None or [x.get('value') for x in select.select('option[selected]')]!=values:
                raise ValueError('APRA filter echo differs')
        views=soup.select('div.search-page')
        if len(views)!=1:raise ValueError('APRA listing container missing')
        view=views[0];text=view.get_text(' ',strip=True)
        totals=re.findall(r'\b(\d+) results\s+Showing (\d+) - (\d+) results\b',text)
        if len(totals)!=1:raise ValueError('APRA total/range missing')
        total,first,last=map(int,totals[0]);current=view.select('nav.pager a[aria-current="page"]')
        nxt=view.select('.pager__item--next a[href]');cards=view.select('div.views-row')
        if total<1 or len(current)!=1 or len(nxt)>1:raise ValueError('APRA invalid pagination')
        current_page=self.page_number(current[0]['href']);final=(total-1)//10
        if not cards:
            if (empty_after!=final or page!=final+1 or current_page!=final or nxt
                    or 'No items match your filters' not in text or (first,last)!=(final*10+1,total)):
                raise ValueError('APRA empty page lacks adjacent final-page evidence')
            return [],False,total
        if (current_page!=page or len(cards)!=min(10,total-page*10) or (first,last)!=(page*10+1,min((page+1)*10,total))
                or bool(nxt)!=(page<final) or nxt and self.page_number(nxt[0]['href'])!=page+1):
            raise ValueError('APRA page/range/next mismatch')
        items=[]
        for art in cards:
            links=art.select('.search-result__title a[href]')
            if len(links)!=1:raise UnsupportedSource('apra_native_card_identity_missing')
            href=links[0]['href'].strip();url=urljoin(self.crawler.base_url,href);p=urlsplit(url)
            if p.scheme!='https' or p.netloc!='www.apra.gov.au' or p.query or p.fragment or not p.path.startswith('/news-and-publications/'):
                raise UnsupportedSource('apra_native_series_URL_shape_changed')
            raw_dates=[]
            for pill in art.select('.anx-pill--split'):
                label=pill.select_one('.anx-pill__label-first');value=pill.select_one('.anx-pill__label-last')
                if label and value and 'published' in label.get_text(strip=True).lower():raw_dates.append(value.get_text(strip=True))
            if len(raw_dates)!=1:raise UnsupportedSource('apra_precise_listing_date_missing')
            posted=g['_parse_display_date'](raw_dates[0])
            try:date.fromisoformat(posted)
            except (ValueError,TypeError):raise UnsupportedSource('apra_precise_listing_date_missing')
            cat=art.select_one('.tag-bungle-type')
            items.append(dict(url=url,href=href,slug=href.strip('/').split('/')[-1],title=links[0].get_text(strip=True),
                date=posted,date_raw=raw_dates[0],category=cat.get_text(strip=True) if cat else 'Statistical publication'))
        if len({x['url'] for x in items})!=len(items):raise UnsupportedSource('apra_repeated_source_identity')
        return items,bool(nxt),total

    def fetch(self,cursor,direction):
        page=cursor['page'] if cursor else 0
        if cursor:
            prior,_,prior_total=self.read(page-1)
            if prior_total!=cursor['total'] or hashlib.sha256(encoded(prior).encode()).hexdigest()!=cursor['anchor']:
                raise ValueError('APRA source page changed; pending retained')
        items,nxt,total=self.read(page);keys=[x['url'] for x in items]
        if cursor and (total!=cursor['total'] or set(keys).intersection(cursor['seen'])):
            raise ValueError('APRA total or membership changed during traversal')
        dates=[x['date'] for x in items];up=bool((cursor or {}).get('date_up')) or any(b>a for a,b in zip(dates,dates[1:]))
        if cursor and dates[0]>cursor['last_date']:up=True
        self.can_read_newest=not up
        if direction=='newest' and up:raise UnsupportedSource('apra_listing_order_changed')
        seen=(cursor or {}).get('seen',[])+keys;exhausted=not nxt
        if exhausted:
            empty,_,end_total=self.read(page+1,empty_after=page)
            if empty or end_total!=total or len(seen)!=total:raise ValueError('APRA source end/count contradicted')
        state=dict(page=page+1,total=total,seen=seen,anchor=hashlib.sha256(encoded(items).encode()).hexdigest(),last_date=dates[-1],date_up=up)
        return Page([Entry(x['url'],x,x['date']) for x in items],state,exhausted,
                    'statistical-series total matched, final pager and adjacent filtered empty page' if exhausted else '')

    def exists(self,entry):
        if self.crawler._conn is None:return False
        store=Store(self.crawler._conn,self.crawler.site_id)
        modern=not store.sqlite or self.crawler._conn_is_libertree();idcol,urlcol=('post_number','meta_url') if modern else ('external_id','url')
        rows=store.execute(f'SELECT {idcol} AS identity FROM documents WHERE site_id=? AND {urlcol}=?',
                           (self.crawler.site_id,entry.key)).fetchall()
        if len(rows)>1 or rows and rows[0]['identity']!=entry.payload['slug']:raise UnsupportedSource('apra_native_storage_identity_changed')
        return bool(rows)

    def materialize(self,item):
        self.exists(Entry(item['url'],item));g=self.crawler.crawl.__globals__;soup=g['_bs'](self.response(item['url'],detail=True))
        if soup is None:raise ValueError('APRA detail parse failed')
        h1=soup.find('h1');title=h1.get_text(strip=True) if h1 else item['title'];abstract=g['_get_abstract'](soup)
        if not title or not abstract or len(abstract)<50:raise ValueError('APRA native detail unavailable; pending retained')
        time=soup.find('time',attrs={'datetime':True});raw_date=time.get('datetime','') if time else ''
        pdf=next((urljoin(self.crawler.base_url,a['href']) for a in soup.find_all('a',href=True) if '.pdf' in a['href'].lower()),None)
        filename=g['_extract_filename'](pdf) if pdf else None
        meta=dict(posted_date=item['date_raw'],published_date_raw=raw_date,category=item['category'])
        if filename:meta['originalFilename']=filename
        return dict(site_id=self.crawler.site_id,external_id=item['slug'],url=item['url'],title=title,abstract=abstract,
            published_date=g['_parse_date'](raw_date),posted_date=item['date'],publisher='Australian Prudential Regulation Authority',
            category=item['category'],pdf_url=pdf,original_filename=filename,metadata=json.dumps(meta,ensure_ascii=False))
