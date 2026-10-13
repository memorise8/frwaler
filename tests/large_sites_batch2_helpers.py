"""Memory-only source fixtures for the three offline candidates."""
import ast
import contextlib
import copy
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from tests.enumeration_fakes import Env

ROOT=Path(__file__).resolve().parents[1]
SITES=['e-stat-go-jp-stat-search','ots-at-pressemappe','etera-ee-browse']
TEXT='Synthetic source abstract 자료 for full offline comparison. '*5


def load(site):
    spec=importlib.util.spec_from_file_location('large_batch_'+site.replace('-','_'),ROOT/'crawler/sites/custom'/(site+'.py'))
    mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod
    with guarded():spec.loader.exec_module(mod)
    cls=next(c for c in vars(mod).values() if isinstance(c,type) and getattr(c,'site_id',None)==site)
    return object.__new__(cls),mod


def raw_plan(site):
    return json.loads((ROOT/'docs/examples/large-sites-batch2'/(site+'.json')).read_text())['sites'][site]


def fixture(site):
    if site==SITES[0]:
        article=''.join(f'<article class="stat-resource_list-item"><a class="stat-item2_parent" href="/stat-search/files?stat_infid={i}" data-value="{i}">Dataset {i}</a><ul><li class="stat-resource_list-detail-item">Survey</li><li class="stat-resource_list-detail-item">Series</li><li class="stat-resource_list-detail-item">調査年月 2025年2月</li><li class="stat-resource_list-detail-item">公開（更新）日 2025年6月12日</li></ul></article>' for i in ['000101','000102'])
        details={f'https://www.e-stat.go.jp/stat-search/files?stat_infid={i}':f'<html><table class="stat-resource_table"><tr><th>調査の概要</th><td>{TEXT}</td></tr><tr><th>政府統計名</th><td>Survey</td></tr><tr><th>調査年月</th><td>2025年2月</td></tr><tr><th>公開年月日時分</th><td>2025年6月13日</td></tr></table><a class="js-dl" href="/download?statInfId={i}" data-file_type="PDF">PDF</a></html>' for i in ['000101','000102']}
        return dict(listing={'items':article},empty={'items':''},details=details)
    if site==SITES[1]:
        rows=[dict(key=f'OTS_20250613_OTS00{i}',titleSlug=f'offline-{i}',date='13.06.2025',time='12:30:00',title={'content':f'Title {i}'},emittentId='54',emittentName='BMI',channel=[{'name':'Government'}]) for i in [1,2]]
        details={'https://core2023.ots.at/api/v1/pressrelease/'+r['key']:{'result':dict(title=r['title'],date='2025-06-13',lead={'content':TEXT},article=[{'type':'text','content':TEXT}],emittentId='54',emittentName='BMI',keywords=['Policy'],attachments=[{'url':'https://www.ots.at/doc/source.pdf'}])} for r in rows}
        return dict(listing={'meta':{'count':2,'totalPages':1},'result':rows},empty={'meta':{'count':2,'totalPages':1},'result':[]},details=details)
    rows=[dict(obj_id=str(i),title=f'Title {i}',date_to='20250200000000',date_mask='YYYYMM',path=f'/zoom/{i}/view?ignored=1',images=1) for i in [101,102]]
    details={f'https://www.etera.ee/api/item/{r["obj_id"]}/meta':{'title':{'values':[r['title']]},'description':{'label':'Abstract','values':[TEXT]},'cf22':{'values':['20250200000000'],'mask':'YYYYMM'},'cf13':{'values':['Vaba']},'classification':{'values':['Book']}} for r in rows}
    return dict(listing={'results':{'count':2,'docs':rows}},empty={'results':{'count':2,'docs':[]}},details=details)


class Transport:
    def __init__(self,site,f=None,*,fallback=False,deny=None,total=None):
        self.site=site;self.f=f or fixture(site);self.calls=[];self.fallback=fallback;self.deny=deny;self.total=total
    def request(self,method,url,*,headers=None,body=None):
        self.calls.append((method,url,copy.deepcopy(headers),body))
        path=urlsplit(url).path
        if path=='/robots.txt':return Response(404,{},b'',url)
        if self.deny and self.deny(method,url):return Response(403,{},b'',url)
        if method=='HEAD':return Response(200,{'Content-Type':'application/pdf','Content-Length':'9999999999','Content-Disposition':"attachment; filename*=UTF-8''report%20source.pdf"},b'',url)
        if '/presseaussendung/' in path:return Response(200,{'Content-Type':'text/html'},f'<html><h1>HTML title</h1><p class="lead">{TEXT}</p><article><article><p>{TEXT}</p></article></article><span>13.06.2025, 12:30:00</span></html>',url)
        if url in self.f['details']:
            value={'result':None} if self.fallback and self.site==SITES[1] else self.f['details'][url]
            return Response(200,{'Content-Type':'text/html' if isinstance(value,str) else 'application/json'},value if isinstance(value,str) else json.dumps(value),url)
        if self.site==SITES[2]:
            assert method=='POST';d=json.loads(body);assert d['filters']==[{'key':'-typ','value':'page'}];assert d['limit']==25;offset=d['start'];assert 'start' not in parse_qs(urlsplit(url).query)
            data=copy.deepcopy(self.f['listing'] if offset==0 else self.f['empty'])
            if self.total is not None:data['results']['count']=self.total
        else:
            assert method=='GET';page=int(parse_qs(urlsplit(url).query)['page'][0]);data=copy.deepcopy(self.f['listing'] if page==1 else self.f['empty'])
            if self.total is not None and self.site==SITES[1]:data['meta']['count']=self.total
        return Response(200,{'Content-Type':'application/json'},json.dumps(data),url)


def run(site,transport=None,plan=None,max_requests=None):
    env=Env(site);c,_=load(site);c.db_conn=env.conn
    # Env supplies the real existing adapter/save routing with a temporary fixture SQLite.
    cls=type(c);c=cls(env.conn,delay=0)
    p=copy.deepcopy(plan or raw_plan(site));p['request_interval_s']=0
    t=transport or Transport(site)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result=env.run(p,lambda u:None,transport=t,crawler=c,**({'max_requests':max_requests} if max_requests else {}))
        return result,env.docs(),t
    finally:env.close()
