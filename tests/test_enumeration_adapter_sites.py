"""Actual source methods, with synthetic HTML/JSON and memory-only detail responses."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.enumeration_adapter_helpers import ROOT, FIXTURES, load_crawler, baseline

LONG = 'Synthetic offline content used only to compare registered parser behavior. ' * 20
DETAIL = ('<html><head><meta property="og:title" content="Synthetic title">'
          '<meta property="og:description" content="'+LONG+'"></head><body>'
          '<main data-history-node-id="123"><h1 class="intro-title">Synthetic title</h1>'
          '<div class="field--name-field-description field--name-body">'+LONG+'</div>'
          '<article><p>'+LONG+'</p></article></main></body></html>')
LISTS = {
'afp-gov-au-news-centre': '<div class="view-solr-news-centre"><div class="view-content"><div class="views-row"><article data-history-node-id="123"><a href="/news-centre/media-release/synthetic">Synthetic title</a><time datetime="2026-10-12">12 October 2026</time></article></div></div></div>',
'africamuseum-be-en': '<main><div class="views-row"><h3><a href="/en/research/news/synthetic">Synthetic title</a></h3><p>October 2026 '+LONG+'</p></div></main>',
'culture-gouv-fr-espace-documentation': '<div class="fr-card" data-internalsearch="{}"><h3 class="fr-card__title"><a href="/espace-documentation/synthetic">Synthetic title</a></h3><time datetime="2026-10-12">12 octobre 2026</time></div>',
'kit-nl-institute': json.dumps([{'id':123,'link':'https://www.kit.nl/institute/synthetic','slug':'synthetic','title':{'rendered':'Synthetic title'},'content':{'rendered':'<p>'+LONG+'</p>'},'date':'2026-10-12T00:00:00'}])}


def actual_case(sid, redirect=False):
    plans=json.loads((ROOT/'docs/examples/adapter-contract-plans.json').read_text())
    sp=site_plan(plans,sid,registered=[sid]);c=load_crawler(sid);direct=load_crawler(sid)
    a=Adapter(sid,sp.section('adapter'),None,crawler=c)
    resp=Response(200,{'Content-Type':'application/json' if sid=='kit-nl-institute' else 'text/html'},LISTS[sid],sp.listing['url'])
    rows=ListingParser(sp.listing['parser'],adapter=a).parse(resp).items
    assert len(rows)==1,(sid,len(rows))
    item=rows[0];calls=[]
    def fetch(url):
        calls.append(url)
        # A site-side redirect: the final response URL differs from the requested item URL.
        return Response(200,{'Content-Type':'text/html'},DETAIL,url+'-moved' if redirect else url)
    result=a.materialize(item,SimpleNamespace(get=fetch))
    if sid=='afp-gov-au-news-centre':
        original=direct._parse_list_page(LISTS[sid],resp.url)[0][0]
        expected=direct._build_paper(original,direct._parse_detail_page(DETAIL,item.url))
    elif sid=='culture-gouv-fr-espace-documentation':
        original=direct._parse_list_page(LISTS[sid],resp.url)[0][0]
        expected=direct._to_paper(original,direct._parse_detail(DETAIL,item.url))
    elif sid=='africamuseum-be-en':
        slug,title,date,snippet,url=direct._parse_list_page(LISTS[sid])[0]
        expected=direct._parse_detail_page(DETAIL,slug,date,snippet,url)
    else:
        expected=direct._parse_item(json.loads(LISTS[sid])[0])
    assert result is not None and expected is not None,sid
    expected=clean_paper(expected);expected.setdefault('site_id',sid)
    assert result==expected,(sid,result,expected)
    assert paper_identity(result)==paper_identity(expected)
    assert paper_identity(result)[0] not in (None,'')
    assert len(calls)==(0 if sid=='kit-nl-institute' else 1)
    return result


class ActualSites(unittest.TestCase):
    def setUp(self):
        def denied(*args, **kwargs):
            raise AssertionError("actual-site fixture attempted network/DB connection")
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'sqlite3.connect'):
            p=patch(name,denied);p.start();self.addCleanup(p.stop)

    def test_afp_registered_pipeline(self):actual_case('afp-gov-au-news-centre')
    def test_africamuseum_tuple_scalar_binding(self):actual_case('africamuseum-be-en')
    def test_culture_registered_pipeline(self):actual_case('culture-gouv-fr-espace-documentation')
    def test_culture_redirect_keeps_item_url(self):
        result=actual_case('culture-gouv-fr-espace-documentation',redirect=True)
        self.assertNotIn('-moved',paper_identity(result)[1])
    def test_kit_registered_json_paper(self):actual_case('kit-nl-institute')

    def test_rejected_24_reclassification(self):
        old=json.loads((FIXTURES/'rejected-24.json').read_text());new=json.loads((ROOT/'docs/examples/adapter-contract-plans.json').read_text())
        self.assertEqual(24,len(old['sites']));passed=[];remaining=[]
        for sid in old['sites']:
            plan={'sites':{sid:new['sites'].get(sid,old['sites'][sid])}}
            try:
                sp=site_plan(plan,sid,registered=[sid]);Adapter(sid,sp.section('adapter'),None,crawler=load_crawler(sid))
            except ValueError as exc:remaining.append((sid,str(exc)))
            else:passed.append(sid)
        self.assertEqual(set(new['sites']),set(passed));self.assertEqual(20,len(remaining))
        self.assertTrue(all(reason=='custom crawler adapter needs detail_method, paper_method or list_returns_paper' for _,reason in remaining))
