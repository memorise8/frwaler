"""Inert queue compilation and metadata-only listing evidence helpers."""
import ast
import hashlib
import json
import re
from datetime import datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup
from delivery.health.scope_inventory import public_url
from delivery.scripts.discovery_structure import inspect, finalize, date_value, PAGE_KEYS

PAGE_KEYS = set(PAGE_KEYS) | {'paged','startpage','cpage','page_idx','page_index','page_number','b_start'}

KINDS = ('페이지 파라미터', 'WordPress REST', 'JSON·검색 API', '사이트맵', 'JS·POST 렌더링', '리다이렉트·도메인', '기타')
CHECKS = ('last_and_after', 'page_parameter', 'first_page_number', 'oldest_sort_or_date_filter',
          'oldest_item', 'total_arithmetic', 'hidden_cap', 'crawler_page_cap', 'domain_migration')


def literal_limits(source, config=None):
    tree = ast.parse(source); result = []; names = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name): names[target.id] = node.value.value
    for node in ast.walk(tree):
        if isinstance(node,ast.Assign) and isinstance(node.value,ast.Call):
            call=node.value
            if isinstance(call.func,ast.Name) and call.func.id=='int' and call.args:
                inner=call.args[0]
                if isinstance(inner,ast.Call) and isinstance(inner.func,ast.Attribute) and inner.func.attr in ('get','getenv') and len(inner.args)>=2:
                    env,default=inner.args[:2]
                    if isinstance(env,ast.Constant) and isinstance(default,ast.Constant) and str(default.value).isdecimal():
                        for target in node.targets:
                            if isinstance(target,ast.Name) and re.search(r'max.*page|page.*(?:limit|cap)',target.id,re.I):
                                result.append(dict(kind='page_cap_environment_default',name=target.id,value=int(default.value),environment=env.value,line=node.lineno,scope_binding_verified=False))
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and re.search(r'max.*page|page.*(?:limit|cap)', target.id, re.I):
                    value = names.get(target.id)
                    if type(value) is int and value > 0:
                        result.append(dict(kind='page_cap_constant', name=target.id, value=value, line=node.lineno, enforced_in_all_routes=False))
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name) and re.search('page', node.target.id, re.I):
            call = node.iter
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id == 'range':
                values = [x.value if isinstance(x, ast.Constant) else names.get(x.id) if isinstance(x, ast.Name) else None for x in call.args]
                if values and all(type(x) is int for x in values):
                    start, stop = (0, values[0]) if len(values)==1 else values[:2]
                    result.append(dict(kind='literal_page_loop', start=start, stop_exclusive=stop, line=node.lineno,
                                       maximum_number=stop-1, source_expression=ast.get_source_segment(source, call), scope_binding_verified=False))
    if config:
        pagination = (config.get('list_page') or {}).get('pagination') or {}
        if pagination: result.append(dict(kind='config_pagination', value=pagination, url=(config.get('list_page') or {}).get('url')))
    return result


def clean_url(value):
    value = value.rstrip('.,;，。)')
    value = re.sub(r'(?<=[=/_])(?:N|K|n)(?:\+1)?(?=[/&?#.]|$)', '1', value)
    # A template still containing a range or an unknown variable is not a URL to call.
    if re.search(r'\{[^}]+\}|\d+\.\.\d+|[가-힣]', value): return None
    return public_url(value)


def family(url):
    p = urlsplit(url)
    path = re.sub(r'/page/\d+/?$', '/page/{page}/', p.path)
    query = [(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if k.lower() not in PAGE_KEYS]
    return urlunsplit(p._replace(path=path, query=urlencode(query), fragment=''))


def compile_plan(row, original, hints, source, config=None):
    text = row['live_check']; urls = []; unresolved = []
    def add(value, role):
        url = clean_url(value)
        if url and not re.search(r'\.(pdf|docx?|xlsx?|zip)(?:$|\?)', urlsplit(url).path, re.I):
            if url not in {x['url'] for x in urls}: urls.append(dict(url=url, provenance=role))
        elif value: unresolved.append(dict(value=value[:300], reason='nonconcrete_or_document_URL_not_requested'))
    explicit = list(re.finditer(r'https?://[^\s<>"\']+', text))
    for match in explicit: add(match[0], 'explicit_live_check_URL')
    # Original scopes remain independent: do not replace several scopes with one endpoint.
    for scope in original.get('scopes', []): add(scope['url'], 'original_registered_scope')
    for match in re.finditer(r'(?<!\S)[?&][^\s<>"\']+', text):
        preceding = [m for m in explicit if m.start()<match.start()]
        anchor = clean_url(preceding[-1][0]) if preceding else (urls[0]['url'] if urls else None)
        if anchor:
            fragment=match[0].rstrip('.,;)');parts=urlsplit(anchor)
            add(urlunsplit(parts._replace(query=fragment[1:] if fragment.startswith('?') else parts.query+fragment)), 'explicit_live_check_query')
    for candidate in hints.get('endpoints', []):
        url = candidate['url']; path = urlsplit(url).path
        # An endpoint named in the requested check is a grounded, inert source hint.
        if len(path)>4 and (path in text or path.rstrip('/').rsplit('/',1)[-1] in text): add(url, 'source_constant_named_in_live_check')
    groups = {}
    for entry in urls:
        groups.setdefault(family(entry['url']), []).append(entry)
    scope_roots=[]
    for node in ast.walk(ast.parse(source)):
        if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='dict':
            values={kw.arg:kw.value.value for kw in node.keywords if isinstance(kw.value,ast.Constant) and isinstance(kw.value.value,str)}
            if 'path' in values and 'view' in values:scope_roots.append(dict(path=values['path'],selector=values['view']))
    return dict(**row, targets=list(groups.values()), unresolved_URLs=unresolved,scope_roots=scope_roots,
                original_scope_urls=[s['url'] for s in original.get('scopes', [])],
                selectors=original['profile']['list_selectors'], pagination_config=original['profile']['pagination_config'],
                selector_hints=[x['selector'] for x in hints.get('listing_selector_hints', [])],
                static_limits=literal_limits(source, config),
                source_sha256=hashlib.sha256(source.encode()).hexdigest(),
                instruction_coverage='Explicit URLs and original scopes; prose conditions retained verbatim and separately assessed')


def page_route(url, text='', pagination=None):
    p = urlsplit(url)
    for key,value in parse_qsl(p.query, keep_blank_values=True):
        if key.lower() in PAGE_KEYS and value.isdecimal():
            return dict(kind='query', key=key, url=url, offset=key.lower() in ('start','offset'), source='explicit_URL')
    if re.search(r'/page/\d+/?$', p.path): return dict(kind='path', url=url, source='explicit_URL', offset=False)
    if re.search(r'/page/(?:[NK]|\d+)/',text):
        target=urlunsplit(p._replace(path=p.path.rstrip('/')+'/page/1/'))
        return dict(kind='path',url=target,source='live_check_path_pattern',offset=False)
    match = re.search(r'[?&]([A-Za-z_][\w-]*)=(?:[NKn]|\d+)(?:\b|&)', text)
    if match and match[1].lower() in PAGE_KEYS:
        return dict(kind='query', key=match[1], url=url, offset=match[1].lower() in ('offset','start'), source='live_check_parameter')
    if pagination and pagination.get('param'):
        key = pagination['param']
        return dict(kind='query', key=key, url=url, offset=key.lower() in ('offset','start'), source='collector_config')
    return None


def at_page(route, number):
    p = urlsplit(route['url'])
    if route['kind']=='path': return public_url(urlunsplit(p._replace(path=re.sub(r'/page/\d+/?$', f'/page/{number}/', p.path))))
    pairs=[(k,v) for k,v in parse_qsl(p.query, keep_blank_values=True) if k!=route['key']]+[(route['key'],str(number))]
    return public_url(urlunsplit(p._replace(query=urlencode(pairs))))


def parse_listing(raw, url, content_type='', plan=None):
    plan=plan or {}; selectors=dict(plan.get('selectors') or {})
    summary=inspect(raw,url,content_type,selectors)
    if summary['format']!='html': return summary
    soup=BeautifulSoup(raw,'html.parser')
    root=soup;root_css=None
    for candidate in plan.get('scope_roots',[]):
        if urlsplit(url).path==candidate['path']:
            selected=soup.select(candidate['selector'])
            if len(selected)==1:root=selected[0];root_css=candidate['selector'];break
    if root_css is None:
        for css in plan.get('selector_hints',[]):
            if not re.fullmatch(r'\.[\w-]*view[\w-]*(?:\.[\w-]+)*',css):continue
            selected=soup.select(css)
            if len(selected)==1 and selected[0].select_one('.view-content,.view-empty'):
                root=selected[0];root_css=css;break
    if root_css:
        scoped=inspect(str(root).encode(),url,content_type,selectors)
        for key in ('items','item_count','item_fingerprint','item_identity_method','oldest_observed','date_order','dated_items','duplicate_id_count'):
            summary[key]=scoped[key]
        summary['source_root_selector']=root_css
    # Queue/code selectors are inert CSS. Only candidates that expose item URLs
    # are usable; parse failure is never accepted as an empty catalogue.
    candidates=[]
    for css in plan.get('selector_hints', []):
        if re.search(r'pager|pagination|script|iframe|header|footer|nav|breadcrumb|\brel\b|aria-current',css,re.I) or css in ('a[href]','small','img','h1'): continue
        try: nodes=root.select(css)
        except Exception: continue
        if not nodes or len(nodes)>10000: continue
        valid=[]
        for node in nodes:
            anchor=node if node.name=='a' and node.has_attr('href') else node.select_one('h2 a[href],h3 a[href],a[href]')
            if not anchor: continue
            link=public_url(urljoin(url,anchor['href']))
            if not link or link==url: continue
            stamp=None
            for stamp_node in node.select('time[datetime],time,.date,.created,small'):
                value=stamp_node.get('datetime') or stamp_node.get_text(' ',strip=True)
                stamp=date_value(value)
                if not stamp:
                    match=re.search(r'\b(\d{1,2})/(\d{1,2})/((?:19|20)\d{2})\b',value)
                    if match:
                        try:stamp=datetime(int(match[3]),int(match[2]),int(match[1])).date().isoformat()
                        except ValueError:pass
                if stamp:break
            valid.append(dict(id=link,date=stamp,date_basis='code_selector_date_node' if stamp else None,identity_basis='source_selector_href'))
        if len(valid)==len(nodes): candidates.append((css,valid))
    # Prefer a dated, unambiguous selector; never merge overlapping containers.
    if candidates and (not summary['items'] or root_css or summary.get('item_identity_method') in ('main article','article')):
        candidates.sort(key=lambda v:(-sum(bool(x['date']) for x in v[1]),-len(v[1])))
        css,items=candidates[0];summary=finalize(summary,items)
        summary['item_identity_method']='static_code_selector:'+css
        summary['selector_scope_requires_review']=True
    main=soup.select_one('main,.view-content,.view-empty,#content')
    empty=root.select_one('.view-empty,.no-results,.no-result,.no-records,.search-empty')
    summary['explicit_empty']=bool(empty and not summary['items'])
    summary['empty_marker']=empty.get('class') if empty else None
    summary['main_present']=main is not None
    headings=[h.get_text(' ',strip=True) for h in soup.select('h1')]
    canonicals=[family(urljoin(url,x['href'])) for x in soup.select('link[rel="canonical"][href]')]
    summary['collection_signature']=hashlib.sha256(json.dumps([headings,canonicals],sort_keys=True).encode()).hexdigest() if headings and canonicals else None
    return summary


def comparison(left, right):
    a,b=left.get('summary',{}),right.get('summary',{})
    return dict(left=left.get('number'),right=right.get('number'),
                body_hash_equal=bool(left.get('sha256') and left.get('sha256')==right.get('sha256')),
                item_IDs_equal=bool(a.get('item_fingerprint') and a.get('item_fingerprint')==b.get('item_fingerprint')),
                item_IDs_different=bool(a.get('item_fingerprint') and b.get('item_fingerprint') and a['item_fingerprint']!=b['item_fingerprint']))


def is_empty_boundary(first, response):
    a,b=first.get('summary',{}),response.get('summary',{})
    return bool(response.get('status')==200 and a.get('item_count') and not b.get('item_count')
                and b.get('explicit_empty') and (a.get('structural_signature')==b.get('structural_signature')
                    or a.get('collection_signature') and a['collection_signature']==b.get('collection_signature')))
