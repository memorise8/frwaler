"""Carabinieri's published Tutti catalogue, with grouped-pager end evidence."""
from datetime import datetime
import hashlib
from pathlib import Path
import re
from urllib.parse import parse_qs,unquote,urljoin,urlsplit

from bs4 import BeautifulSoup
from .incremental import Entry,Page,UnsupportedSource,encoded
from .incremental_adapters import Adapter


class CarabinieriAdapter(Adapter):
    rows=20
    endpoint='https://www.carabinieri.it/in-vostro-aiuto/informazioni/comunicati-stampa'

    def __init__(self,crawler,*,month=None):
        if month is not None and (not isinstance(month,str) or not re.fullmatch(r'\d{6}',month)
                                  or datetime.strptime(month,'%Y%m').strftime('%Y%m')!=month):
            raise ValueError('invalid Carabinieri source month')
        super().__init__(crawler,{'endpoint':self.endpoint,'d':month or 'all','rows':self.rows})
        self.contract=hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()).hexdigest()

    def url(self,page):
        return self.endpoint+(f'/!page/page/{page}' if page>1 else '')+'?d='+self.scope['d']

    def html(self,url,*,detail=False):
        self.http_requests+=1
        if detail:self.detail_requests+=1
        r=self.crawler._request(url,retries=1)
        if r is None:raise ValueError('Carabinieri source fetch failed')
        # Optional </td> tags in the publisher HTML need the HTML5 table rules.
        return r.text,BeautifulSoup(r.text,'html5lib')

    def pager_number(self,href):
        p=urlsplit(urljoin(self.endpoint,href));base=urlsplit(self.endpoint)
        if p.scheme!=base.scheme or p.netloc!=base.netloc or parse_qs(p.query)!={'d':[self.scope['d']]}:
            raise ValueError('Carabinieri pager scope mismatch')
        if p.path==base.path:return 1
        match=re.fullmatch(re.escape(base.path)+r'/!page/page/([1-9]\d*)',p.path)
        if not match:raise ValueError('Carabinieri unknown pager route')
        return int(match.group(1))

    def read(self,page,*,allow_empty=False):
        raw,soup=self.html(self.url(page));table=soup.select_one('table.tabella.footable');pager=soup.select_one('.PagerLista .sf_pagerNumeric')
        if table is None or pager is None or table.caption is None or table.caption.get_text(strip=True)!='Comunicati Stampa':
            raise ValueError('Carabinieri listing structure missing')
        headers=[n.get_text(' ',strip=True) for n in table.select('thead th')]
        if headers!=['Data','Titolo','Fonte','Descrizione']:raise ValueError('Carabinieri table schema changed')
        records=self.crawler._parse_list_page(raw)
        links=pager.select('a[href]');numbers=[self.pager_number(a['href']) for a in links]
        current=pager.select('.sf_PagerCurrent')
        empty=table.select_one('td[colspan]')
        explicit_empty=empty is not None and empty.get_text(' ',strip=True)=='Nessun elemento trovato con i parametri indicati.'
        if not records:
            if not allow_empty or not explicit_empty or current or any(n>=page for n in numbers):
                raise ValueError('Carabinieri empty response is not verified exhaustion')
            return [],False,hashlib.sha256(encoded([]).encode()).hexdigest()
        if explicit_empty or len(current)!=1 or current[0].get_text(strip=True)!=str(page) or self.pager_number(current[0]['href'])!=page:
            raise ValueError('Carabinieri selected page mismatch')
        data_rows=table.select('tbody tr')
        if len(records)!=len(data_rows) or not 1<=len(records)<=self.rows:
            raise ValueError('Carabinieri missing/duplicate list rows')
        entries=[];base=urlsplit(self.endpoint)
        for record in records:
            p=urlsplit(record['url']);slug=p.path.rsplit('/',1)[-1]
            if (p.scheme!=base.scheme or p.netloc!=base.netloc or p.path.rsplit('/',1)[0]!=base.path or p.query or not slug):
                raise UnsupportedSource('carabinieri_list_identity_route_unverified')
            try:date=datetime.strptime(record['raw_date'],'%d/%m/%Y %H:%M')
            except ValueError as exc:raise UnsupportedSource('carabinieri_precise_list_date_unavailable') from exc
            if self.scope['d']!='all' and date.strftime('%Y%m')!=self.scope['d']:
                raise ValueError('Carabinieri month filter ignored')
            entries.append(Entry(slug,record,date.isoformat()))
        if len({e.key for e in entries})!=len(entries):raise ValueError('Carabinieri repeated list identities')
        dates=[e.date for e in entries]
        if dates!=sorted(dates,reverse=True):raise UnsupportedSource('carabinieri_newest_order_unverified')
        forward=any(n>page for n in numbers)
        if forward and (page+1 not in numbers or len(records)!=self.rows):
            raise ValueError('Carabinieri broken forward pager')
        return entries,forward,hashlib.sha256(encoded(records).encode()).hexdigest()

    def fetch(self,cursor,direction):
        page=(cursor or {}).get('page',1)
        if cursor:
            _,forward,anchor=self.read(page-1)
            if anchor!=cursor['anchor'] or not forward:raise ValueError('Carabinieri previous page changed; reconcile required')
        entries,forward,anchor=self.read(page,allow_empty=page==1)
        if not entries:
            return Page([],{'page':page,'empty':True},True,'explicit empty source table on initial query')
        if cursor and set(e.key for e in entries).intersection(cursor['ids']):raise ValueError('Carabinieri adjacent pages repeat')
        if cursor and entries[0].date>cursor['last_date']:raise UnsupportedSource('carabinieri_cross_page_order_changed')
        exhausted=False
        if not forward:
            following,_,_=self.read(page+1,allow_empty=True)
            if following:raise ValueError('Carabinieri terminal pager contradicted by next page')
            exhausted=True
        return Page(entries,{'page':page+1,'anchor':anchor,'ids':[e.key for e in entries],'last_date':entries[-1].date},exhausted,
                    'final published pager plus explicit empty adjacent page' if exhausted else '')

    def materialize(self,record):
        raw,soup=self.html(record['url'],detail=True);body=soup.select_one('.docTesto');date_node=soup.select_one('.docData')
        if body is None or date_node is None:raise ValueError('Carabinieri detail body/date envelope missing')
        canonical=soup.select_one('link[rel="canonical"][href]')
        if canonical and canonical.get('href'):
            target=urlsplit(urljoin(record['url'],canonical['href']));wanted=urlsplit(record['url'])
            if target.netloc!=wanted.netloc or unquote(target.path).rstrip('/')!=unquote(wanted.path).rstrip('/'):
                raise ValueError('Carabinieri detail canonical mismatch')
        match=re.search(r'\b\d{2}/\d{2}/\d{4} \d{2}:\d{2}\b',date_node.get_text(' ',strip=True))
        if not match or match.group()!=record['raw_date']:raise ValueError('Carabinieri detail/list date mismatch')
        day=datetime.strptime(match.group(),'%d/%m/%Y %H:%M').date().isoformat()
        _,_,location=self.crawler._parse_detail(raw)
        abstract=re.sub(r'\s+',' ',body.get_text(' ',strip=True)).strip()
        return {'site_id':self.crawler.site_id,'external_id':urlsplit(record['url']).path.rsplit('/',1)[-1],
                'url':record['url'],'title':record['title'],'abstract':abstract,'published_date':day,
                'posted_date':record['raw_date'],'publisher':record.get('source') or None,
                'category':'Comunicati Stampa','metadata':encoded({'location':location,'list_summary':record.get('summary','')})}
