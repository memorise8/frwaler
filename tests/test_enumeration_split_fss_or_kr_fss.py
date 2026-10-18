"""fss-or-kr-fss: enumeration split regression (collector-splits 방식).

crawl() 은 바꾸지 않았다. 합성 HTML(fixture)로 원래 crawl() 이 저장하는 paper 와
enumeration 경로(_enumeration_items → 상세 → _enumeration_paper)가 내는 paper 가
같은지(특히 external_id=nttId·post_number=목록 번호·url) 확인한다. 네트워크·프로세스·DB 없음.
기존 수집기의 쪽 상한(_MAX_PAGES=200)은 틀의 last_page_backward 끝 판정으로 대신한다.
"""
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
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture

SITE = 'fss-or-kr-fss'
NET_METHODS = ['_curl_get']


class EnumerationSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('io')
            raise AssertionError('split test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        saved, listing_calls = [], []

        def transport(url, *args, **kwargs):
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        for name in NET_METHODS:
            setattr(crawler, name, transport)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(saved), result)
        self.assertEqual(2, len(listing_calls))
        return saved

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        resp = Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], fixture['listing_url'])
        items = parser.parse(resp).items
        self.assertEqual(2, len(items))
        self.assertEqual(['241777', '241700'], [i.key for i in items])

        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected), 'fixture must save two papers through crawl()')
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(clean_paper(p)) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(['241777', '241700'], [p['external_id'] for p in actual])
        self.assertEqual(['20934', '20933'], [p['post_number'] for p in actual])
        self.assertTrue(all('pageIndex=1' in p['url'] for p in actual))

    def test_short_error_detail_skip_is_preserved(self):
        f = copy.deepcopy(self.fixture)
        f['details'] = {u: '<html><body>error</body></html>' for u in f['details']}
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = self.fixture['listing_url']
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        try:
            err = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                        '<html><body><p>Site under maintenance</p></body></html>', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertTrue(err.extra.get('parse_error'))
            self.assertFalse(err.extra.get('explicit_empty', False))

    def test_shipped_plan_completes_end_to_end(self):
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url)
        result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'241777', '241700'}, {d['post_number'] for d in docs})  # DB post_number ← external_id(nttId)

    def test_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        tree = ast.parse(code)
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and any(
            isinstance(n, ast.FunctionDef) and n.name == 'crawl' for n in x.body))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual.get(name), name)


if __name__ == '__main__':
    unittest.main()
