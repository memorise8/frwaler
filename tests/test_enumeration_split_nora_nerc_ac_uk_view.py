"""L3b-20261018: enumeration split for nora-nerc-ac-uk-view.
crawl() reads the NOC division index, then every year page
/view/division/noc/YYYY.default.html (no pagination) and each eprint page.
Plan: date_range_split, yearly segments from 1994 (no splitting); ?page>1 of a
year page is declared empty and proved by div.ep_view_page. Papers are the
_parse_detail_page() conversion (external_id = eprints.eprintid).
Memory-only; no network or subprocess."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load

SITE = 'nora-nerc-ac-uk-view'
INDEX = 'https://nora.nerc.ac.uk/view/division/noc/'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_raw():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


def year_url(year):
    return f'{INDEX}{year}.default.html'


class NoraSplit(unittest.TestCase):
    def setUp(self):
        self.fx = fixture()
        self.plan = plan_raw()
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def body(self, url, fx=None):
        fx = fx or self.fx
        if url == INDEX:
            return fx['index_body']
        m = re.search(r'/(\d{4})\.default\.html', url)
        if m:
            return fx['years'].get(m.group(1))
        return fx['details'].get(url)

    def native(self, fx=None):
        crawler, _ = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = lambda url, timeout=60: self.body(url, fx)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, fx=None, redirect=False):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        papers, keys = [], []
        for year in ('2031', '2030'):
            url = p.listing['url'].replace('{segment}', year) + f'?year={year}&page=1'
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.body(url, fx), url))
            keys += page.keys

            def fetch(u):
                return Response(200, {'Content-Type': 'text/html'}, self.body(u, fx), u + '?moved=1' if redirect else u)
            for item in page.items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return keys, papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        ids = ['90004', '90003', '90002', '90001']
        self.assertEqual(ids, [p['external_id'] for p in expected])
        keys, actual = self.pipeline()
        self.assertEqual(ids, keys)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_detail_redirect_preserves_registered_identity(self):
        self.assertEqual([clean_paper(p) for p in self.native()], self.pipeline(redirect=True)[1])

    def test_short_abstract_skip_is_preserved(self):
        fx = json.loads(json.dumps(self.fx).replace(self.fx['long_text'], 'Short.'))
        self.assertEqual([], self.native(fx))
        self.assertEqual([], self.pipeline(fx)[1])

    def test_page_two_empty_and_error_pages_differ(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = year_url(2031)
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fx['years']['2031'], url + '?year=2031&page=2'))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url + '?year=2031&page=1'))
        self.assertEqual([], foreign.items)
        self.assertFalse(foreign.extra.get('explicit_empty', False))
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items({'error': 'x'}, url)

    def test_segments_are_yearly_from_1994_to_now(self):
        seg = self.plan['sites'][SITE]['segments']
        self.assertEqual(('range', 'year', '1994', 'now'), (seg['mode'], seg['granularity'], seg['from'], seg['to']))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


class NoraSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fx = fixture()
        plan = copy.deepcopy(plan_raw()['sites'][SITE])
        plan['request_interval_s'] = 0
        plan['segments']['from'] = '2030'
        plan['segments']['to'] = '2031-12-31'

        def handler(url):
            if urlsplit(url).path == '/robots.txt':
                return Response(404, {}, b'')
            base = url.split('?')[0]
            m = re.search(r'/(\d{4})\.default\.html', base)
            body = fx['years'].get(m.group(1)) if m else fx['details'].get(base)
            if body is None:
                return Response(404, {'Content-Type': 'text/html'}, '<html>not found</html>', url)
            return Response(200, {'Content-Type': 'text/html'}, body, url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            crawler = cls(env.conn)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=crawler)
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(4, len(docs))


if __name__ == '__main__':
    unittest.main()
