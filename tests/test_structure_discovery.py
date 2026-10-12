"""Meaningful reconnaissance contract tests; no databases or live transport."""
import ast
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from delivery.scripts.discovery_structure import inspect, date_value, same_listing, query_replace
from delivery.scripts.discover_collection_structure import scout_site, FEATURES, ReconTransport, StopRequested

URL='https://source.example/list'


def page(n, *, wrap=False, shell=False):
    if shell:return '<html><title>Source</title><h1>Unavailable</h1><div id="app"></div></html>'
    current=0 if wrap else n
    ids=[5,4] if current==0 else [2,1] if current==2 else []
    body=''.join(f'<div class="views-row"><a href="/item/{i}">{i}</a><time datetime="2020-01-0{i}"></time></div>' for i in ids)
    nav='<a rel="next" href="?page=1">Next</a><a rel="last" href="?page=2">Last</a>' if current==0 else ''
    return f'<html><title>Source</title><link rel="canonical" href="{URL}"><h1>Items</h1><main>{body}</main>{nav}</html>'


class FakeTransport:
    def __init__(self,out,mapping):self.out=Path(out);self.mapping=mapping;self.calls=[];self.holds=[]
    def fetch(self,url):
        self.calls.append(url);raw=self.mapping.get(url)
        if raw is None:return dict(url=url,status=403,error='access_refused',checked_at='2026-10-12T00:00:00+00:00'),None
        return dict(url=url,status=200,content_type='text/html',checked_at='2026-10-12T00:00:00+00:00',sha256=hashlib.sha256(raw.encode()).hexdigest()),raw.encode()
    def hold(self,host,evidence):self.holds.append((host,evidence))


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.row=dict(site_id='example',initial_tier='높음',basis='estimate',source_sha256='fixture',profile={'list_selectors':{}},scopes=[dict(url=URL,provenance='original')])
    def run_site(self,mapping,budget=24):
        transport=FakeTransport(self.tmp.name,mapping);return scout_site(self.row,transport,budget),transport

    def test_last_advertised_page_and_adjacent_end_measured(self):
        result,t=self.run_site({URL:page(0),URL+'?page=2':page(2),URL+'?page=3':page(3)})
        scope=result['scopes'][0];self.assertEqual(set(scope['features']),set(FEATURES))
        self.assertTrue(scope['features']['last_page']['verified']);self.assertEqual(scope['strategy'],'마지막 페이지부터 거꾸로')
        self.assertEqual(scope['features']['oldest_item']['item']['date'],'2020-01-01')
        self.assertFalse(scope['features']['oldest_item']['global_oldest_proven']);self.assertEqual(len(t.calls),4)

    def test_wraparound_is_flagged_not_clean_completion(self):
        result,t=self.run_site({URL:page(0),URL+'?page=2':page(2),URL+'?page=3':page(3,wrap=True)})
        self.assertTrue(result['scopes'][0]['features']['limits_and_wraparound']['wraparound_or_clamp'])
        self.assertEqual(result['status'],'needs_review')

    def test_error_shell_not_accepted_as_empty_source_end(self):
        result,t=self.run_site({URL:page(0),URL+'?page=2':page(2),URL+'?page=3':page(3,shell=True)})
        self.assertFalse(result['scopes'][0]['features']['last_page']['verified']);self.assertEqual(result['status'],'needs_review')

    def test_denied_entry_stops_without_alternate_route(self):
        result,t=self.run_site({})
        self.assertEqual(t.calls,[URL]);self.assertEqual(result['status'],'unmeasurable')
        self.assertTrue(all(v['status']=='unmeasurable' for k,v in result['scopes'][0]['features'].items() if k!='measurement_origin'))

    def test_budget_is_enforced_and_not_marked_finished(self):
        result,t=self.run_site({URL:page(0),URL+'?page=2':page(2)},budget=2)
        self.assertEqual(len(t.calls),2);self.assertEqual(result['status'],'needs_review')

    def test_dates_keep_unknown_relative_and_year_precision(self):
        self.assertIsNone(date_value('3 days ago'));self.assertIsNone(date_value('2020'))
        self.assertIsNone(date_value('2020-02-31'));self.assertEqual(date_value('18 Jun 2026'),'2026-06-18')

    def test_sort_form_preserves_scope_and_excludes_sensitive_forms(self):
        source='<form action="/list" method="get"><input name="category" value="press"><select name="sort"><option value="date_desc">Newest</option><option value="date_asc">Oldest</option></select><select name="year"><option value="2020">2020</option></select></form>'
        parsed=inspect(source,URL+'?category=press&year=2020');self.assertEqual(len(parsed['oldest_options']),1)
        self.assertIn('category=press',parsed['oldest_options'][0]['url']);self.assertEqual(len(parsed['date_filters']),1)
        self.assertEqual(inspect(source.replace('name="category"','name="session_token"'),URL)['oldest_options'],[])

    def test_alumni_navigation_is_not_an_oldest_sort_control(self):
        parsed=inspect('<a href="/formation/associations-danciens-eleves">Associations d’anciens élèves</a>',URL)
        self.assertEqual(parsed['oldest_options'],[])

    def test_page_changes_preserve_filters(self):
        self.assertFalse(same_listing(URL+'?category=press',URL+'?category=reports&page=3'))
        self.assertEqual(query_replace(URL+'?category=press&page=1','page',3),URL+'?category=press&page=3')

    def test_sitemap_date_is_not_publication_date(self):
        raw='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://source.example/item/1</loc><lastmod>2020-01-01</lastmod></url></urlset>'
        parsed=inspect(raw,URL,'application/xml');self.assertEqual(parsed['items'][0]['date_basis'],'sitemap_lastmod_not_publication')

    def test_no_db_or_crawler_execution_in_runner(self):
        root=Path(__file__).resolve().parents[1]
        tree=ast.parse((root/'delivery/scripts/discover_collection_structure.py').read_text())
        called={n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        self.assertFalse(called & {'crawl','_save_paper','open_db','connect','execute','insert_document'})

    def test_stop_file_prevents_new_requests(self):
        Path(self.tmp.name,'STOP').touch()
        with self.assertRaises(StopRequested):self.run_site({URL:page(0)})

    def test_binary_document_body_is_never_read_or_written(self):
        transport=ReconTransport(self.tmp.name,self.tmp.name);session=MagicMock();response=MagicMock()
        response.is_redirect=False;response.status_code=200;response.headers={'Content-Type':'application/pdf'}
        session.get.return_value.__enter__.return_value=response
        with patch('delivery.scripts.discover_collection_structure.public_address',return_value='1.1.1.1'),patch('requests.Session',return_value=session):
            observed,raw=transport.fetch(URL)
        self.assertIsNone(raw);response.iter_content.assert_not_called()
        self.assertEqual(observed['error'],'non_listing_content_type_body_not_downloaded')

    def test_access_hold_prevents_further_host_requests(self):
        transport=ReconTransport(self.tmp.name,self.tmp.name);transport.hold('source.example',{'reason':'access_refused'})
        with patch('requests.Session',side_effect=AssertionError('network forbidden')):
            observed,raw=transport.fetch(URL)
        self.assertEqual(observed['error'],'host_access_hold');self.assertIsNone(raw)


if __name__=='__main__':unittest.main()
