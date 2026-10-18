"""L3b-20261018 (ext:path_paging): enumeration split for carabinieri-it-in-vostro-aiuto.
List .../comunicati-stampa?d=all (first page) and .../comunicati-stampa/!page/page/N?d=all
(plan listing.url {page} + first_page_url, params d=all), 20 rows; past the last page the
source answers 200 with the empty table.tabella (listing_marker). post_number = URL slug
(unchanged). crawl() is unchanged; the reference is crawl()'s own save argument."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'carabinieri-it-in-vostro-aiuto'
BASE = 'https://www.carabinieri.it/in-vostro-aiuto/informazioni/comunicati-stampa'


def html_resp(body, url, status=200):
    return Response(status, {'Content-Type': 'text/html; charset=utf-8'}, body, url)


class Split(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {'sites': {SITE: split_plan(SITE)}}
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, lists = [], []

        def curl(url, retries=3):
            if url in fixture['details']:
                return fixture['details'][url]
            lists.append(url)
            return fixture['listing_body'] if len(lists) == 1 else fixture['empty_body']
        crawler._curl_get = curl
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        self.assertEqual([BASE + '?d=all', BASE + '/!page/page/2?d=all'], lists)
        return captured

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            html_resp(fixture['listing_body'], BASE + '?d=all')).items
        self.assertEqual(2, len(items))
        papers = []

        def get(u):
            self.assertIn(u, fixture['details'], 'undeclared detail request')
            return html_resp(fixture['details'][u], u)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=get))
            if paper is not None:
                papers.append(paper)
        return papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['sintetico-alfa', 'sintetico-beta'], [p['post_number'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False)
                       .replace(self.fixture['long_text'], 'Breve.').replace(self.fixture['summary_text'], 'Breve'))
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_page_urls_put_the_page_in_the_path(self):
        from crawler.enumeration.strategies import Strategy
        p = site_plan(self.plan, SITE, registered=[SITE])
        strat = Strategy(SimpleNamespace(plan=p, fetcher=None, parser=None, store=None, site_id=SITE))
        self.assertEqual(BASE + '?d=all', strat.page_url(1))
        self.assertEqual(BASE + '/!page/page/2?d=all', strat.page_url(2))

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = BASE + '/!page/page/500?d=all'
        page = parser.parse(html_resp(self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(html_resp('<html><body><p>Servizio non disponibile</p></body></html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_original_methods_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


class SplitRun(unittest.TestCase):
    """The shipped plan through the real SiteRunner: one page, then the empty table."""

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)
        requested = []

        def handler(url):
            requested.append(url)
            if urlsplit(url).path == '/robots.txt':
                return Response(404, {}, b'', url)
            if url in fixture['details']:
                return html_resp(fixture['details'][url], url)
            if url == BASE + '?d=all':
                return html_resp(fixture['listing_body'], url)
            if '/!page/page/' in url:
                return html_resp(fixture['empty_body'], url)
            return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['sintetico-alfa', 'sintetico-beta'], sorted(d['post_number'] for d in docs))
        self.assertFalse([u for u in requested if 'page=' in u])


if __name__ == '__main__':
    unittest.main()
