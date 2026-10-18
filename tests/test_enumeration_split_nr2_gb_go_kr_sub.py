"""NR2-20261018: gb-go-kr-sub plan over the registered crawler's own pure parsers
(_parse_list_items / _build_paper; crawl() and the collector file unchanged), memory-only.
Identity stays the crawler's rule: external_id = the list's display number V_NUM
(식별값 목록 번호 의존 — 기존 규칙 유지, 사용자 위임 10-19)."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture

SITE = 'gb-go-kr-sub'


class RegisteredPlan(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def native(self, fixture):
        crawler, _ = load(SITE)
        captured, lists = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def get(url, *a, **k):
            if url in fixture['details']:
                return fixture['details'][url]
            lists.append(url)
            return fixture['listing_body'] if len(lists) == 1 else fixture['empty_body']
        with patch.object(crawler, '_curl_get', get), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured

    def parser(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        return p, adapter, ListingParser(p.listing['parser'], adapter=adapter)

    def pipeline(self, fixture):
        p, adapter, parser = self.parser()
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        items = parser.parse(Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)).items
        self.assertEqual(2, len(items))

        def fetch(u):
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][u], u)
        return [x for x in (adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items) if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.fixture)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.fixture)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([('504', expected[0]['url']), ('503', expected[1]['url'])],
                         [paper_identity(p) for p in actual])

    def test_empty_listing_and_error_page_are_distinct(self):
        p, _, parser = self.parser()
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertTrue(page.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(err.extra.get('parse_error'))

    def test_collector_file_is_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['original_method_source_sha256'], actual)


class RegisteredPlanRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        fixture = split_fixture(SITE)

        def end(url):
            from urllib.parse import parse_qs, urlsplit
            start = parse_qs(urlsplit(url).query).get('Start', ['0'])[0]
            return Response(200, {'Content-Type': 'text/html'},
                            fixture['listing_body'] if start == '0' else fixture['empty_body'], url)
        result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'504', '503'}, {d['post_number'] for d in docs})


if __name__ == '__main__':
    unittest.main()
