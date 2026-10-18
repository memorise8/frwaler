"""L3b-20261018 (ext:headers): enumeration split for hud-govt-nz-documents.
GET /documents/search?q=&_documenttypes[]=32&start=N (offset, 12 rows) answers JSON
{content: HTML fragment, resultsSentence} only with X-Requested-With (plan request_headers).
Past the last page: 200 with a pagination-only fragment; far past it 'There has been an
error' + 'Sorry, no results'. crawl() is unchanged; the reference is crawl()'s own save argument."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'hud-govt-nz-documents'
LIST = 'https://www.hud.govt.nz/documents/search'


def json_resp(body, url):
    return Response(200, {'Content-Type': 'application/json; charset=utf-8'}, body, url)


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
        """crawl() fed from the fixture: page 1 = listing, page 2 = the empty past-the-end page."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, lists = [], []

        def curl(url, extra_headers=None, retries=3):
            if url in fixture['details']:
                return fixture['details'][url]
            self.assertEqual({'X-Requested-With': 'XMLHttpRequest'}, extra_headers)
            lists.append(url)
            return fixture['listing_body'] if len(lists) == 1 else fixture['empty_body']
        crawler._curl_get = curl
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        self.assertEqual(2, len(lists))
        return captured

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        url = LIST + '?q=&_documenttypes%5B%5D=32&start=0'
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(json_resp(fixture['listing_body'], url)).items
        self.assertEqual(3, len(items))
        fetched, papers = [], []

        def get(u):
            self.assertIn(u, fixture['details'], 'undeclared detail request')
            fetched.append(u)
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][u], u)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=get))
            if paper is not None:
                papers.append(paper)
        self.assertEqual(sorted(fixture['details']), sorted(fetched))
        return papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(3, len(expected))
        # The list's PDF link carries '?m=...', so crawl() keeps no list pdf_url and the
        # detail-less card's slug comes from its title (unchanged rule).
        self.assertEqual(['synthetic-report-alpha', 'synthetic-report-beta', 'synthetic-sheet-gamma'],
                         [p['external_id'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_empty_pages_and_error_pages_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = LIST + '?q=&_documenttypes%5B%5D=32&start=96'
        for body in (self.fixture['empty_body'], self.fixture['far_body']):
            page = parser.parse(json_resp(body, url))
            self.assertEqual([], page.items)
            self.assertTrue(page.extra.get('explicit_empty'))
        for body in ('<html><body><p>Site under maintenance</p></body></html>',
                     json.dumps({'error': 'This endpoint accepts AJAX requests only.'}),
                     json.dumps({'content': '<div>other</div>', 'resultsSentence': '0'})):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(json_resp(body, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_plan_sends_the_ajax_header_to_the_site_host_only(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        self.assertEqual({'X-Requested-With': 'XMLHttpRequest'}, resolve_headers(p.raw))
        self.assertEqual(['www.hud.govt.nz'], list(header_hosts(p.raw)))

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
    """The shipped plan through the real SiteRunner (temporary SQLite of tests.enumeration_fakes.Env)."""

    def setUp(self):
        self.fixture = split_fixture(SITE)

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = self.fixture
        plan = dict(split_plan(SITE), request_interval_s=0)
        seen_headers = []

        class Transport:
            def request(self, method, url, *, headers=None, data=None):
                parts = urlsplit(url)
                if parts.path == '/robots.txt':
                    return Response(404, {}, b'', url)
                if url in fixture['details']:
                    return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
                if parts.path == '/documents/search':
                    seen_headers.append((headers or {}).get('X-Requested-With'))
                    start = int(parse_qs(parts.query).get('start', ['0'])[0])
                    return json_resp(fixture['listing_body'] if start == 0 else fixture['empty_body'], url)
                return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            p = env.plan(plan)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, None, crawler=cls(env.conn), transport=Transport(),
                                 extra_headers=resolve_headers(p.raw), header_hosts=header_hosts(p.raw))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['synthetic-report-alpha', 'synthetic-report-beta', 'synthetic-sheet-gamma'],
                         sorted(d['post_number'] for d in docs))
        self.assertTrue(seen_headers and all(h == 'XMLHttpRequest' for h in seen_headers))


if __name__ == '__main__':
    unittest.main()
