"""Actual source methods, with synthetic HTML/JSON and memory-only detail responses."""
import copy
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import AdapterUngatedNetwork
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


def actual_case(sid, redirect=False, listing=None, detail=DETAIL):
    """Every listed item through the contract adapter vs. the registered methods called
    in crawl() order (crawl() passes the pre-request item URL to the detail parser)."""
    listing=listing or LISTS[sid]
    plans=json.loads((ROOT/'docs/examples/adapter-contract-plans.json').read_text())
    sp=site_plan(plans,sid,registered=[sid]);c=load_crawler(sid);direct=load_crawler(sid)
    a=Adapter(sid,sp.section('adapter'),None,crawler=c)
    resp=Response(200,{'Content-Type':'application/json' if sid=='kit-nl-institute' else 'text/html'},listing,sp.listing['url'])
    rows=ListingParser(sp.listing['parser'],adapter=a).parse(resp).items
    calls=[];results=[]
    def fetch(url):
        calls.append(url)
        # A site-side redirect: the final response URL differs from the requested item URL.
        return Response(200,{'Content-Type':'text/html'},detail,url+'-moved' if redirect else url)
    for index,item in enumerate(rows):
        result=a.materialize(item,SimpleNamespace(get=fetch))
        if sid=='culture-gouv-fr-espace-documentation':
            original=direct._parse_list_page(listing,resp.url)[0][index]
            expected=direct._to_paper(original,direct._parse_detail(detail,original['url']))
        elif sid=='africamuseum-be-en':
            slug,title,date,snippet,url=direct._parse_list_page(listing)[index]
            expected=direct._parse_detail_page(detail,slug,date,snippet,url)
        else:
            expected=direct._parse_item(json.loads(listing)[index])
        if expected is None:
            assert result is None,(sid,result)
        else:
            expected=clean_paper(expected);expected.setdefault('site_id',sid)
            assert result==expected,(sid,result,expected)
            assert paper_identity(result)==paper_identity(expected)
            assert paper_identity(result)[0] not in (None,'')
        results.append(result)
    assert len(calls)==(0 if sid=='kit-nl-institute' else len(rows))
    return results


def two_items(sid):
    """The fixture listing with a second, distinct item."""
    if sid=='kit-nl-institute':
        rows=json.loads(LISTS[sid]);second=json.loads(LISTS[sid].replace('synthetic','second'))[0]
        second['id']=124
        return json.dumps(rows+[second])
    if sid=='africamuseum-be-en':
        return LISTS[sid].replace('</main>','')+LISTS[sid].replace('synthetic','second').replace('<main>','')
    return LISTS[sid]+LISTS[sid].replace('synthetic','second')


class ActualSites(unittest.TestCase):
    def setUp(self):
        def denied(*args, **kwargs):
            raise AssertionError("actual-site fixture attempted network/DB connection")
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'sqlite3.connect'):
            p=patch(name,denied);p.start();self.addCleanup(p.stop)

    def test_afp_not_a_candidate(self):
        # Its registered detail parser shells out to curl for a PDF link without a
        # filename; the guard stops the whole site, so it is not a contract candidate.
        plans=json.loads((ROOT/'docs/examples/adapter-contract-plans.json').read_text())
        self.assertNotIn('afp-gov-au-news-centre',plans['sites'])
        sid='afp-gov-au-news-centre'
        cfg={'type':'crawler','contract_version':1,'list_method':'_parse_list_page','list_args':['html','url'],
             'list_result':[0],'detail_method':'_parse_detail_page','detail_args':['html','url'],
             'paper_method':'_build_paper','paper_args':['item','detail']}
        a=Adapter(sid,cfg,None,crawler=load_crawler(sid))
        resp=Response(200,{'Content-Type':'text/html'},LISTS[sid],'https://www.afp.gov.au/news-centre')
        item=ListingParser({'type':'adapter','url_field':'url','listing_marker':'.view-solr-news-centre'},adapter=a).parse(resp).items[0]
        detail=('<html><body><main data-history-node-id="9"><h1>T</h1><p>'+LONG+'</p>'
                '<a href="/download?f=report.pdf">pdf</a></main></body></html>')
        with patch('subprocess.Popen._execute_child',side_effect=AssertionError('process actually spawned')), \
                self.assertRaisesRegex(AdapterUngatedNetwork,r'^adapter_ungated_network:subprocess$'):
            a.materialize(item,SimpleNamespace(get=lambda url:Response(200,{'Content-Type':'text/html'},detail,url)))
    def test_africamuseum_tuple_scalar_binding(self):self.assertEqual(1,len(actual_case('africamuseum-be-en')))
    def test_culture_registered_pipeline(self):self.assertEqual(1,len(actual_case('culture-gouv-fr-espace-documentation')))
    def test_kit_registered_json_paper(self):self.assertEqual(1,len(actual_case('kit-nl-institute')))

    def test_redirect_keeps_item_url(self):
        for sid in ('africamuseum-be-en','culture-gouv-fr-espace-documentation'):
            with self.subTest(sid=sid):
                for result in actual_case(sid,redirect=True):
                    self.assertNotIn('-moved',paper_identity(result)[1])

    def test_multiple_items_per_site(self):
        for sid in ('africamuseum-be-en','culture-gouv-fr-espace-documentation','kit-nl-institute'):
            with self.subTest(sid=sid):
                results=actual_case(sid,listing=two_items(sid))
                self.assertEqual(2,len(results))
                self.assertEqual(2,len({paper_identity(r)[1] for r in results}))

    def test_detail_pdf_link_without_filename(self):
        # Culture/AfricaMuseum resolve such links without any network call (the guard
        # would stop the run otherwise); the paper still matches the registered methods.
        pdf=DETAIL.replace('</main>','<a href="/download?f=report.pdf">PDF</a><a href="/download/123">Download</a></main>')
        with patch('subprocess.Popen._execute_child',side_effect=AssertionError('process actually spawned')):
            for sid in ('africamuseum-be-en','culture-gouv-fr-espace-documentation'):
                with self.subTest(sid=sid):
                    self.assertEqual(1,len(actual_case(sid,detail=pdf)))

    def test_short_abstract_not_filtered_by_contract(self):
        # Known limit (M2): crawl()-inline minimum-abstract filters are not part of the
        # contract. Culture's crawl() skips abstracts under MIN_ABSTRACT_CHARS; the
        # contract path still returns the registered paper.
        sid='culture-gouv-fr-espace-documentation'
        short=('<html><head><meta property="og:title" content="Synthetic title"></head><body><main>'
               '<h1>Synthetic title</h1><p>Short.</p></main></body></html>')
        result=actual_case(sid,detail=short)[0]
        self.assertLess(len(result.get('abstract') or ''),load_crawler(sid).MIN_ABSTRACT_CHARS)
        # AfricaMuseum filters inside its registered detail parser, so the contract
        # path sees None exactly like crawl() (counted as a contract failure by the runner).
        self.assertEqual([None],actual_case('africamuseum-be-en',listing=LISTS['africamuseum-be-en'].replace(LONG,'Short.'),detail=short))

    def test_rejected_24_reclassification(self):
        old=json.loads((FIXTURES/'rejected-24.json').read_text());new=json.loads((ROOT/'docs/examples/adapter-contract-plans.json').read_text())
        self.assertEqual(24,len(old['sites']));passed=[];remaining=[]
        for sid in old['sites']:
            plan={'sites':{sid:new['sites'].get(sid,old['sites'][sid])}}
            try:
                sp=site_plan(plan,sid,registered=[sid]);Adapter(sid,sp.section('adapter'),None,crawler=load_crawler(sid))
            except ValueError as exc:remaining.append((sid,str(exc)))
            else:passed.append(sid)
        self.assertEqual(set(new['sites']),set(passed));self.assertEqual(3,len(passed));self.assertEqual(21,len(remaining))
        self.assertTrue(all(reason=='custom crawler adapter needs detail_method, paper_method or list_returns_paper' for _,reason in remaining))
