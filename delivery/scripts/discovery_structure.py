"""Pure bounded catalogue inspection. No crawler execution, DB or document writes."""
from datetime import datetime
import hashlib
import json
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

from bs4 import BeautifulSoup
from delivery.health.scope_inventory import public_url, SENSITIVE

PAGE_KEYS = {'page','p','pageindex','pageno','pagenum','page_no','page_num','pagenumber','currentpage','start','offset'}
DATE_KEYS = re.compile(r'date|year|from|until|since|start|end|기간|년도|연도', re.I)
OLDEST = re.compile(r'\boldest\b|\bascending\b|date[^&<> ]*asc|created[^&<> ]*asc|오래된|과거순|오름차순|plus anciens?\b|älteste', re.I)
LAST = re.compile(r'last|최종|마지막|끝|derni|letzte|últim', re.I)
MONTHS = {m.casefold():i for i in range(1,13) for m in (datetime(2000,i,1).strftime('%B'),datetime(2000,i,1).strftime('%b'))}
MONTHS.update({'jan':1,'fév':2,'fev':2,'mar':3,'avr':4,'mai':5,'jun':6,'juin':6,'jul':7,'juil':7,'aoû':8,'aou':8,'sep':9,'oct':10,'nov':11,'déc':12,'dec':12})


def date_value(text):
    text=str(text or '').strip()
    m=re.search(r'\b((?:19|20)\d{2})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})',text)
    if m:
        try:return datetime(*map(int,m.groups())).date().isoformat()
        except ValueError:return None
    m=re.search(r'\b([A-Za-z]+)\s+(\d{1,2}),?\s+((?:19|20)\d{2})\b',text)
    if m and m[1].casefold() in MONTHS:
        try:return datetime(int(m[3]),MONTHS[m[1].casefold()],int(m[2])).date().isoformat()
        except ValueError:pass
    m=re.search(r'\b(\d{1,2})\s+([^\W\d_]+)\.?\s+((?:19|20)\d{2})\b',text)
    if m and m[2].casefold() in MONTHS:
        try:return datetime(int(m[3]),MONTHS[m[2].casefold()],int(m[1])).date().isoformat()
        except ValueError:pass
    return None


def query_replace(url, name, value):
    u=urlsplit(url);pairs=[(k,v) for k,v in parse_qsl(u.query,keep_blank_values=True) if k!=name]
    return public_url(urlunsplit(u._replace(query=urlencode(pairs+[(name,str(value))]))))


def page_parameter(url):
    values=[(k,int(v)) for k,v in parse_qsl(urlsplit(url).query) if k.casefold() in PAGE_KEYS and v.isdecimal()]
    return values[0] if len(values)==1 else None


def same_listing(a,b):
    x,y=urlsplit(a),urlsplit(b)
    if (x.scheme,x.netloc,x.path)!=(y.scheme,y.netloc,y.path):return False
    clean=lambda u:sorted((k,v) for k,v in parse_qsl(u.query,keep_blank_values=True) if k.casefold() not in PAGE_KEYS)
    return clean(x)==clean(y)


def same_sort_scope(a,b):
    x,y=urlsplit(a),urlsplit(b)
    if (x.scheme,x.netloc,x.path)!=(y.scheme,y.netloc,y.path):return False
    ignored=lambda k:k.casefold() in PAGE_KEYS or bool(re.search(r'sort|order',k,re.I))
    original={k:v for k,v in parse_qsl(x.query,keep_blank_values=True) if not ignored(k) and v}
    proposed={k:v for k,v in parse_qsl(y.query,keep_blank_values=True) if not ignored(k) and v}
    return original==proposed


def blank():
    return dict(format='unknown',items=[],item_count=0,item_identity_method=None,totals=[],pagination=[],
        last_links=[],next_links=[],oldest_options=[],date_filters=[],sitemaps=[],anomalies=[],
        item_fingerprint=None,oldest_observed=None,parse_limit_reached=False,login_required=False)


def finalize(out,items):
    # Summaries retain identifiers/dates only, never article or document bodies.
    out['item_count']=len(items)
    keys=[i['id'] for i in items]
    out['duplicate_id_count']=len(keys)-len(set(keys))
    out['item_fingerprint']=hashlib.sha256(json.dumps(keys,ensure_ascii=False).encode()).hexdigest() if keys else None
    dated=[i for i in items if i.get('date')]
    out['oldest_observed']=min(dated,key=lambda i:i['date']) if dated else (items[-1] if items else None)
    out['dated_items']=len(dated)
    stamps=[i.get('date') for i in items]
    pairs=[(a,b) for a,b in zip(stamps,stamps[1:]) if a and b]
    out['date_order']=dict(increasing=sum(a<b for a,b in pairs),decreasing=sum(a>b for a,b in pairs),missing=len(items)-len(dated))
    out['items']=items[:8]+items[-8:] if len(items)>16 else items
    return out


def inspect_json(data,url):
    out=blank();out['format']='json';items=[];arrays=[]
    def walk(value,path='',depth=0):
        if depth>5:return
        if isinstance(value,dict):
            for k,v in value.items():
                here=path+'/'+k
                if k.lower() in ('total','total_count','totalcount','numfound','count','numberofelements','totalelements'):
                    if isinstance(v,(int,float)) and not isinstance(v,bool):out['totals'].append(dict(value=v,source=here,role='reported_count_scope_requires_review'))
                    elif isinstance(v,dict) and isinstance(v.get('value'),int):out['totals'].append(dict(value=v['value'],source=here+'/value',role='reported_count_scope_requires_review'))
                if k.lower() in ('next','last','first','prev') and isinstance(v,str) and public_url(urljoin(url,v)):
                    out['pagination'].append(dict(url=urljoin(url,v),label=k,parameter=page_parameter(urljoin(url,v))))
                    if k.lower()=='last':out['last_links'].append(urljoin(url,v))
                    if k.lower()=='next':out['next_links'].append(urljoin(url,v))
                walk(v,here,depth+1)
        elif isinstance(value,list) and value and isinstance(value[0],dict):arrays.append((path,value))
    walk(data)
    priorities=['/results','/response/docs','/hits/hits','/result/results','/data','/items','/records','/content']
    arrays.sort(key=lambda x:priorities.index(x[0]) if x[0] in priorities else 99)
    for path,rows in arrays[:1]:
        out['parse_limit_reached']=len(rows)>10000
        for r in rows[:10000]:
            d=r.get('_source',r);key=next((d.get(k) for k in ('id','docid','identifier','uuid','halId_s','url','uri','guid','_id') if isinstance(d.get(k),(str,int))),r.get('_id'))
            if key is None:continue
            dates=[(k,v) for k,v in d.items() if re.search(r'date|published|created|submitted|issued',k,re.I) and isinstance(v,str)]
            chosen=next(((k,date_value(v)) for k,v in dates if date_value(v)),(None,None))
            items.append(dict(id=str(key),date=chosen[1],date_basis=chosen[0],identity_basis=path+'/id_or_uri'))
        out['item_identity_method']='source_json_identifier'
    return finalize(out,items)


def inspect_xml(raw,url):
    out=blank();out['format']='xml'
    try:root=ET.fromstring(raw)
    except ET.ParseError:out['anomalies'].append('invalid_xml');return out
    tag=lambda e:e.tag.rsplit('}',1)[-1]
    if tag(root) in ('urlset','sitemapindex'):
        children=list(root);items=[]
        for child in children[:10000]:
            fields={tag(e):(e.text or '').strip() for e in child};loc=public_url(fields.get('loc'))
            if loc:items.append(dict(id=loc,date=date_value(fields.get('lastmod')),date_basis='sitemap_lastmod_not_publication',identity_basis='sitemap_loc'))
        out['parse_limit_reached']=len(children)>10000;out['item_identity_method']='sitemap_loc'
        out['totals']=[dict(value=len(children),source=tag(root),role='sitemap_entries_not_scoped_document_total')]
        if tag(root)=='sitemapindex':out['sitemaps']=[i['id'] for i in items[:20]]
        out['xml_root']=tag(root);return finalize(out,items)
    out['anomalies'].append('xml_contract_requires_specialized_review');return out


def inspect_html(raw,url,selectors=None):
    out=blank();out['format']='html';soup=BeautifulSoup(raw,'html.parser');selectors=selectors or {}
    out['page_title']=(soup.title.get_text(' ',strip=True)[:160] if soup.title else None)
    out['structural_signature']=hashlib.sha256(json.dumps(dict(
        headings=[h.get_text(' ',strip=True) for h in soup.select('h1')],
        canonical=[x.get('href') for x in soup.select('link[rel="canonical"]')],
        views=sorted({c for x in soup.select('[class]') for c in x.get('class',[]) if c.startswith(('view-id-','view-display-id-'))})
    ),sort_keys=True).encode()).hexdigest()
    out['login_required']=bool(soup.select('input[type="password"]') and not soup.select('article,.views-row'))
    links=soup.select('a[href]')
    for a in links:
        href=public_url(urljoin(url,a.get('href','')));label=a.get_text(' ',strip=True)[:100]
        if not href:continue
        role=' '.join([label,a.get('title',''),' '.join(a.get('rel',[])),' '.join(a.get('class',[])), ' '.join(a.parent.get('class',[]) if a.parent else [])])
        number=page_parameter(href)
        if same_listing(url,href) and number:
            out['pagination'].append(dict(url=href,label=label,parameter=number))
            if LAST.search(role):out['last_links'].append(href)
            if re.search(r'next|다음|suivant|nächste',role,re.I):out['next_links'].append(href)
        if 'sitemap' in href.lower() and (href.endswith('.xml') or '.xml?' in href):out['sitemaps'].append(href)
        if OLDEST.search(role) and href!=url and same_sort_scope(url,href):out['oldest_options'].append(dict(url=href,evidence=role[:160],method='published_link'))
    for form in soup.select('form'):
        method=form.get('method','get').lower();action=public_url(urljoin(url,form.get('action') or url))
        values=[];sensitive=False
        for field in form.select('input[name],select[name]'):
            name=field['name'];typ=field.get('type','text').lower()
            if SENSITIVE.search(name):sensitive=True;continue
            if typ in ('submit','button','file','password'):continue
            if field.name=='select':
                opts=field.select('option');selected=next((x for x in opts if x.has_attr('selected')),opts[0] if opts else None)
                value=selected.get('value',selected.get_text()) if selected else ''
            else:
                if typ in ('checkbox','radio') and not field.has_attr('checked'):continue
                value=field.get('value','')
            values.append((name,value))
            if DATE_KEYS.search(name) and (typ=='date' or field.name=='select' or re.search(r'date|year|from|until|since',name,re.I)):
                options=[dict(value=o.get('value',''),label=o.get_text(' ',strip=True)[:80]) for o in field.select('option')][:100]
                out['date_filters'].append(dict(name=name,type=typ,options=options,method=method,action=action))
        if method!='get' or not action or sensitive:continue
        for field in form.select('select[name]'):
            for option in field.select('option'):
                label=option.get_text(' ',strip=True);value=option.get('value',label)
                if OLDEST.search(label+' '+value):
                    pairs=[(k,v) for k,v in values if k!=field['name']]+[(field['name'],value)]
                    parsed=urlsplit(action);target=public_url(urlunsplit(parsed._replace(query=urlencode(pairs))))
                    if target and same_sort_scope(url,target):out['oldest_options'].append(dict(url=target,evidence=label+' '+value,method='published_GET_form'))
    candidates=[];method=None
    if selectors.get('item_container'):
        try:candidates=soup.select(selectors['item_container']);method='configured_item_container' if candidates else None
        except Exception:out['anomalies'].append('invalid_configured_selector')
    if not candidates:
        for css in ('main .views-row','.view-content .views-row','.media-releases .views-row','main article','main table tbody tr','.board_list tbody tr','.board-list tbody tr','.search-results li','.results-list li','article'):
            candidates=soup.select(css)
            if candidates:method=css;break
    items=[]
    out['parse_limit_reached']=len(candidates)>10000
    for row in candidates[:10000]:
        a=None
        if selectors.get('item_link'):
            try:a=row.select_one(selectors['item_link'])
            except Exception:pass
        if a is None:a=row.select_one('h2 a[href],h3 a[href],.title a[href],.subject a[href],a[href]')
        if a is None:continue
        key=public_url(urljoin(url,a.get('href','')))
        if not key or key==url or page_parameter(key) and same_listing(url,key):continue
        node=row.select_one('time[datetime]');stamp=date_value(node.get('datetime')) if node else None;basis='time_datetime' if stamp else None
        if stamp is None and selectors.get('date'):
            try:node=row.select_one(selectors['date'])
            except Exception:node=None
            stamp=date_value(node.get_text(' ',strip=True)) if node else None;basis='configured_date' if stamp else None
        if stamp is None:
            node=row.select_one('.date,.created,.published,[class*=date],time')
            stamp=date_value(node.get_text(' ',strip=True)) if node else None;basis='date_label' if stamp else None
        if stamp is None:stamp=date_value(row.get_text(' ',strip=True));basis='row_text_unverified_date_role' if stamp else None
        items.append(dict(id=key,date=stamp,date_basis=basis,identity_basis='source_item_href'))
    out['item_identity_method']=method
    text=soup.get_text(' ',strip=True)
    for pattern in (r'총\s*([\d,]+)\s*건',r'(?:전체|Total)\s*[:：]?\s*([\d,]+)\s*(?:건|records|results|items)',r'([\d,]+)\s+(?:results|résultats|documents|records)\b',r'of\s+([\d,]+)\s+(?:results|items|records)'):
        for match in re.finditer(pattern,text,re.I):out['totals'].append(dict(value=int(match[1].replace(',','')),source=match[0][:100],role='visible_count_scope_requires_review'))
    out['javascript_shell']=not candidates and bool(soup.select('#app,#root,app-root,[ng-app]'))
    out['pagination']=list({v['url']:v for v in out['pagination']}.values())[:100]
    out['oldest_options']=list({v['url']:v for v in out['oldest_options']}.values())[:10]
    out['date_filters']=out['date_filters'][:20];out['totals']=out['totals'][:20]
    return finalize(out,items)


def inspect(raw,url,content_type='',selectors=None):
    if isinstance(raw,bytes):text=raw.decode('utf-8',errors='replace')
    else:text=raw
    if urlsplit(url).path=='/robots.txt' and re.search(r'(?im)^\s*(?:User-agent|Sitemap|Disallow):',text):
        out=blank();out['format']='robots'
        out['sitemaps']=[u for value in re.findall(r'(?im)^\s*Sitemap:\s*(\S+)',text) if (u:=public_url(urljoin(url,value)))]
        return out
    if 'json' in content_type or text.lstrip().startswith(('{','[')):
        try:return inspect_json(json.loads(text),url)
        except (ValueError,TypeError):pass
    if 'xml' in content_type or text.lstrip().startswith('<?xml'):return inspect_xml(text,url)
    return inspect_html(text,url,selectors)
