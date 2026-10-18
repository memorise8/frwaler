"""L3b-20261018 (ext:headers): enumeration split for hud-govt-nz-stats-and-insights.
GET /documents/search?q=&sort=latest&start=N&count=12 (offset, 12 rows, whole document library)
answers JSON {content: HTML fragment, resultsSentence} only with X-Requested-With (plan
request_headers). Papers come from the list rows (no detail request), post_number=None and
external_id = detail slug / file name / title slug exactly as crawl() saves them; metadata
search_start_offset is the page's own start. crawl() is unchanged."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'hud-govt-nz-stats-and-insights'
LIST = 'https://www.hud.govt.nz/documents/search'


def json_resp(body, url):
    return Response(200, {'Content-Type': 'application/json; charset=utf-8'}, body, url)


def page_url(start):
    return f'{LIST}?q=&sort=latest&count=12&start={start}'


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

    def native(self):
        """crawl() fed from the fixture: start=0 = listing, start=12 = empty past-the-end page."""
        crawler, module = load(SITE)
        captured, lists = [], []

        def curl(url, extra_headers=None, timeout=30, retries=3):
            lists.append(url)
            return self.fixture['listing_body'] if len(lists) == 1 else self.fixture['empty_body']
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', curl), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        self.assertEqual([f'{LIST}?q=&sort=latest&start={n}&count=12' for n in (0, 12)], lists)
        return captured

    def items(self, start=0, body=None):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        return adapter, parser.parse(json_resp(body or self.fixture['listing_body'], page_url(start))).items

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['synthetic-update-alpha', 'Synthetic-Beta'], [p['external_id'] for p in expected])
        self.assertTrue(all(p['post_number'] is None for p in expected))
        adapter, items = self.items(0)
        self.assertEqual(2, len(items))  # the card without any link is dropped, as crawl() does
        actual = [adapter.materialize(i, SimpleNamespace(get=None)) for i in items]
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_search_start_offset_is_the_page_offset(self):
        adapter, items = self.items(2088)
        papers = [adapter.materialize(i, SimpleNamespace(get=None)) for i in items]
        self.assertEqual({2088}, {json.loads(p['metadata'])['search_start_offset'] for p in papers})

    def test_empty_pages_and_error_pages_are_distinct(self):
        for body in (self.fixture['empty_body'], self.fixture['far_body']):
            _, items = self.items(2100, body)
            self.assertEqual([], items)
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        self.assertTrue(parser.parse(json_resp(self.fixture['empty_body'], page_url(2100))).extra.get('explicit_empty'))
        for body in ('<html><body><p>Site under maintenance</p></body></html>',
                     json.dumps({'error': 'This endpoint accepts AJAX requests only.'}),
                     json.dumps({'content': '<div>other</div>', 'resultsSentence': '0'})):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(json_resp(body, page_url(0)))
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

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)
        seen_headers = []

        class Transport:
            def request(self, method, url, *, headers=None, data=None):
                parts = urlsplit(url)
                if parts.path == '/robots.txt':
                    return Response(404, {}, b'', url)
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
        self.assertEqual(['Synthetic-Beta', 'synthetic-update-alpha'], sorted(d['post_number'] for d in docs))
        self.assertTrue(seen_headers and all(h == 'XMLHttpRequest' for h in seen_headers))


if __name__ == '__main__':
    unittest.main()
