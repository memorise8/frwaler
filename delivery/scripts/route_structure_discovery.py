"""Read-only static hints for measured gaps, never a substitute for observations.

Extract literal listing endpoints and selectors without executing a crawler or
loading a database. Public access holds veto proposed alternative HTTP routes.
"""
import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from delivery.health.results import atomic_json, utc_now
from delivery.health.scope_inventory import public_url, SENSITIVE
from delivery.scripts.review_structure_discovery import audit_site

ROOT=Path(__file__).resolve().parents[2]
LIST_ROLE=re.compile(r'list|search|start|endpoint|scopes|entr|catalog|_urls|api',re.I)
LIST_FUNCTION=re.compile(r'list|search|crawl|index|extract.*(?:paths|links)',re.I)


def constant(node,names):
    """Evaluate only inert literal structure; never calls, comprehensions or eval."""
    if isinstance(node,ast.Constant):return node.value
    if isinstance(node,ast.Name):return names.get(node.id)
    if isinstance(node,ast.Attribute) and isinstance(node.value,ast.Name) and node.value.id in ('self','cls'):
        return names.get(node.attr)
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
        left,right=constant(node.left,names),constant(node.right,names)
        if isinstance(left,str) and isinstance(right,str):return left+right
    if isinstance(node,(ast.List,ast.Tuple)):
        return [constant(v,names) for v in node.elts]
    if isinstance(node,ast.Dict):
        result={}
        for key,value in zip(node.keys,node.values):
            if key is None:continue
            k=constant(key,names)
            if isinstance(k,(str,int)):result[k]=constant(value,names)
        return result
    if isinstance(node,ast.JoinedStr):
        parts=[]
        for part in node.values:
            value=constant(part.value,names) if isinstance(part,ast.FormattedValue) and not part.format_spec else constant(part,names)
            if not isinstance(value,(str,int)):return None
            parts.append(str(value))
        return ''.join(parts)
    return None


def source_hints(source,base_url,config=None):
    tree=ast.parse(source);statements=list(tree.body)
    for node in tree.body:
        if isinstance(node,ast.ClassDef):statements.extend(node.body)
    names={};assignments=[]
    for _ in range(3):
        for node in statements:
            if isinstance(node,(ast.Assign,ast.AnnAssign)):
                value=constant(node.value,names)
                targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target,ast.Name):
                        names[target.id]=value
                        if _==2:assignments.append((target.id,value,node.lineno))
    base=next((names[n] for n in ('_BASE_URL','BASE_URL','base_url') if isinstance(names.get(n),str)),base_url)
    endpoints=[];selectors=[];unresolved=[]
    def collect(role,value,line=None,config_path=None):
        if SENSITIVE.search(role):return
        if isinstance(value,dict):
            for k,v in value.items():collect(role+'.'+str(k),v,line,config_path)
        elif isinstance(value,list):
            for i,v in enumerate(value):collect(role+'['+str(i)+']',v,line,config_path)
        elif isinstance(value,str):
            url=public_url(value)
            if not url and value.startswith('/') and base:url=public_url(urljoin(base,value))
            if url:endpoints.append(dict(url=url,source_role=role,line=line,config_path=config_path,
                scope_equivalence_verified=False,static_hint_only=True))
    for name,value,line in assignments:
        if LIST_ROLE.search(name):
            collect(name,value,line)
            if value is None:unresolved.append(dict(name=name,line=line,reason='runtime_or_nonliteral_expression'))
    for function in ast.walk(tree):
        if not isinstance(function,(ast.FunctionDef,ast.AsyncFunctionDef)) or not LIST_FUNCTION.search(function.name):continue
        for node in ast.walk(function):
            if not isinstance(node,ast.Call) or not isinstance(node.func,ast.Attribute):continue
            if node.func.attr not in ('select','select_one') or not node.args:continue
            selector=constant(node.args[0],names)
            if isinstance(selector,str) and 0<len(selector)<=300:
                selectors.append(dict(selector=selector,function=function.name,line=node.lineno,static_hint_only=True))
    if isinstance(config,dict):
        for key in ('list_page','api','api_config','pagination','start_urls'):
            if key in config:collect('config.'+key,config[key],config_path=key)
        listing=config.get('list_page') or {}
        for name,value in (listing.get('selectors') or {}).items():
            if isinstance(value,str):selectors.append(dict(selector=value,source_role='config.list_page.selectors.'+name,static_hint_only=True))
    endpoints=list({(e['url'],e['source_role']):e for e in endpoints}.values())
    selectors=list({s['selector']:s for s in selectors}.values())
    return dict(endpoints=endpoints,listing_selector_hints=selectors,unresolved_constants=unresolved,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),measured=False)


def security_hold(result,held):
    for scope in result.get('scopes',[]):
        for url in (scope.get('url'),scope.get('resolved_url')):
            if url and urlsplit(url).hostname in held:return True
        for reason in scope.get('blocked_reasons',[]):
            if re.search(r'challenge|access_refus|access_hold|authentication|login|rate_limited',reason,re.I):return True
    return False


def route(row,result,hints,held):
    if result is None:return dict(site_id=row['site_id'],status='awaiting_first_pass',network_authorized_by_plan=False)
    audit=audit_site(result)
    if security_hold(result,held):
        return dict(site_id=row['site_id'],status='observed_access_hold',network_authorized_by_plan=False,
                    reason='No alternate API, mirror or browser challenge bypass after access refusal',quality_issues=audit['issues'])
    if audit['audited_status']!='needs_review':
        return dict(site_id=row['site_id'],status='no_followup_required_by_current_audit',network_authorized_by_plan=False)
    observed={r['url'] for r in result.get('requests',[])}
    candidates=[x for x in hints['endpoints'] if x['url'] not in observed]
    all_urls=[s['url'] for s in result.get('scopes',[])]+[x['url'] for x in candidates]
    families=[]
    if any('/wp-json/' in u for u in all_urls):families.append('WordPress_REST_list_metadata_only')
    if any('/api/' in u or '/api.' in u or 'api.' in urlsplit(u).netloc for u in all_urls):families.append('native_API_contract_review')
    if hints['listing_selector_hints']:families.append('existing_HTML_selector_review')
    if any(s.get('features',{}).get('date_filters',{}).get('controls') for s in result.get('scopes',[])):families.append('advertised_date_filter_measurement')
    if any(r.get('summary',{}).get('javascript_shell') for r in result.get('requests',[])):families.append('conditional_Playwright_for_nonsecurity_JS_UI')
    if not families:families=['manual_retained_markup_and_published_navigation_review']
    return dict(site_id=row['site_id'],status='followup_required',families=families,
                requests_used=audit['requests_already_used'],remaining_budget=audit['remaining_requests_before_30'],
                quality_issues=audit['issues'],candidate_endpoints=candidates[:30],
                existing_selector_hints=hints['listing_selector_hints'],network_authorized_by_plan=False,
                next_gate='Verify original scope and public contract before any bounded follow-up request')


def build(out):
    from crawler.sites import CRAWLERS
    out=Path(out);manifest=json.loads((out/'manifest.json').read_text());held=json.loads((out/'access-holds.json').read_text()) if (out/'access-holds.json').exists() else {}
    hints={};routes=[];cache={}
    for row in manifest['sites']:
        site=row['site_id'];cls=CRAWLERS[site];path=Path(cls.crawl.__code__.co_filename)
        if path not in cache:cache[path]=path.read_text()
        cp=getattr(cls,'_config_path',None);config=json.loads(Path(cp).read_text()) if cp else None
        hint=source_hints(cache[path],getattr(cls,'base_url',None),config);hints[site]=hint
        file=out/'sites'/(site+'.json');result=json.loads(file.read_text()) if file.exists() else None
        routes.append(route(row,result,hint,held))
    at=utc_now();summary=dict(updated_at=at,registered=len(routes),counts=dict(Counter(r['status'] for r in routes)),
        families=dict(Counter(f for r in routes for f in r.get('families',[]))),network_requests=0,database_connections=0)
    atomic_json(out/'source-hints.json',dict(updated_at=at,static_only=True,sites=hints))
    atomic_json(out/'followup-routing.json',dict(**summary,sites=routes));print(json.dumps(summary))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--out',required=True);args=p.parse_args();build(args.out)
