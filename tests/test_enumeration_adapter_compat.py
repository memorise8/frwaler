"""17 original plans: compare e2ebfc9 and new adapter boundaries, offline.

Registered parsers are not changed. Spies return identical synthetic metadata to
exercise plan-specific call bindings, finish/identity, and generic/custom paths.
Actual parser results are separately tested in test_enumeration_adapter_sites.
"""
import copy
import dataclasses
import json
import unittest

from crawler.enumeration import adapters, parsing, plan
from crawler.enumeration.http import Response
from tests.enumeration_adapter_helpers import FIXTURES, load_crawler, baseline


def normalize(value):
    if dataclasses.is_dataclass(value):return normalize(dataclasses.asdict(value))
    if isinstance(value,dict):return {k:normalize(v) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [normalize(v) for v in value]
    if value is None or isinstance(value,(str,int,float,bool)):return value
    return str(value)


def compare(test,sid,plans):
    oldplan=baseline('plan').site_plan(plans,sid,registered=plans['sites'])
    newplan=plan.site_plan(plans,sid,registered=plans['sites'])
    test.assertEqual(oldplan.raw,newplan.raw);test.assertEqual(oldplan.fingerprint,newplan.fingerprint)
    outcomes=[]
    for am,pm in [(baseline('adapters'),baseline('parsing')),(adapters,parsing)]:
        crawler=load_crawler(sid);calls=[]
        payload={'detail_url':'https://example.test/doc/123','url':'https://example.test/doc/123','external_id':'123','title':'Synthetic title','published_date':'2026-10-12','listed_date':'2026-10-12'}
        if sid=='krivet-re-kr':
            payload.update(url='https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=123',detail_url='https://www.krivet.re.kr/kor/sub.do?menuSn=23&pstNo=123')
        paper={**payload,'site_id':sid,'abstract':'Synthetic abstract','metadata':'{"post_number":"native-rule"}'}
        cfg=plans['sites'][sid]['adapter']
        def spy_list(*args):calls.append(['list',normalize(args)]);return [copy.deepcopy(payload)]
        def spy_detail(*args):calls.append(['detail',normalize(args)]);return copy.deepcopy(paper)
        if cfg.get('list_method'):setattr(crawler,cfg['list_method'],spy_list)
        elif hasattr(crawler,'_config'):crawler._extract_list_items=spy_list
        if cfg.get('detail_method'):setattr(crawler,cfg['detail_method'],spy_detail)
        elif hasattr(crawler,'_config'):crawler._fetch_detail=spy_detail
        a=am.Adapter(sid,cfg,None,crawler=crawler)
        response=Response(200,{'Content-Type':'text/html'},'<main><h1>Synthetic</h1></main>','https://example.test/list')
        if oldplan.strategy!='sitemap':
            parsed=pm.ListingParser(oldplan.listing['parser'],adapter=a).parse(response)
            test.assertEqual(1,len(parsed.items));item=parsed.items[0]
            parsed=normalize(parsed)
        else:
            item=pm.Item('123','https://example.test/doc/123',payload=copy.deepcopy(payload));parsed=None
        class Fetcher:
            def get(self,url):
                calls.append(['fetch',url])
                return Response(200,{'Content-Type':'text/html'},'<main>Synthetic detail</main>',url)
        result=a.materialize(item,Fetcher())
        test.assertIsNotNone(result)
        outcomes.append({'page':parsed,'paper':result,'identity':am.paper_identity(result),'calls':calls,'needs_detail':a.needs_detail(),'identity_uses_list_fields':a.identity_uses_list_fields()})
    test.assertEqual(outcomes[0],outcomes[1])


class LegacyPlans(unittest.TestCase):
    pass

for filename in ['plans-v2.json','plans-codex.json']:
    plans=json.loads((FIXTURES/filename).read_text())
    for sid in plans['sites']:
        def test(self,sid=sid,plans=plans):compare(self,sid,plans)
        setattr(LegacyPlans,'test_'+sid.replace('-','_'),test)
