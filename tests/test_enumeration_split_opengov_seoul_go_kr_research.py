"""opengov-seoul-go-kr-research: enumeration split regression (collector-splits 방식).

crawl() 은 바꾸지 않았다. 합성 HTML(fixture)로 원래 crawl() 이 저장하는 paper 와
enumeration 경로(_enumeration_items → 상세 → _enumeration_paper)가 내는 paper 가
같은지(특히 external_id·post_number·url) 확인한다. 네트워크·프로세스·DB 없음.
기존 수집기의 쪽 상한(_MAX_PAGES=200 × 50건)은 사이트 목록의 숨은 상한(10,000건, 2026-10-18 실측:
items_per_page=50 에서 200쪽이 끝, 날짜 필터로는 그 이전 항목이 더 나옴)과 같아서, 예시 plan 은
date_range_split(startDate/endDate, rangeDate=custom, 연 단위)로 끝을 판정한다. external_id=nid.
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

SITE = 'opengov-seoul-go-kr-research'
NET_NAMES = ['_curl_get']
KEYS = ['37111951', '37073641', '37000001']
DOC_POST_NUMBERS = ['37111951', '37073641']  # 3번째는 초록 100자 미만이라 crawl() 도 건너뜀


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

    def native(self):
        fixture = self.fixture
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
        for name in NET_NAMES:
            setattr(crawler, name, transport)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(saved), result)
        self.assertLessEqual(len(listing_calls), 2)
        return saved

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self):
        fixture = self.fixture
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        resp = Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], fixture['listing_url'])
        items = parser.parse(resp).items
        self.assertEqual(KEYS, [i.key for i in items])

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
        self.assertEqual(DOC_POST_NUMBERS, [paper_identity(p)[0] for p in actual])
        self.assertTrue(actual[0]['pdf_url'].endswith('rid=R1&fid='))  # .pdf 첨부가 표지 이미지보다 우선

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

    def test_plan_uses_inclusive_year_segments(self):
        site = self.plan['sites'][SITE]
        self.assertEqual('date_range_split', site['strategy'])
        flt = site['segments']['filter']
        self.assertEqual(('startDate', 'endDate', True), (flt['from_param'], flt['to_param'], flt['to_inclusive']))
        self.assertEqual('custom', site['listing']['params']['rangeDate'])

    def test_shipped_plan_completes_end_to_end(self):
        plan = copy.deepcopy(self.plan['sites'][SITE])
        plan['segments']['from'] = '2026'  # 합성 실행은 한 구간만
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url)
        result, docs = run_split(SITE, end=end, plan=plan)
        self.assertTrue(result['status'].startswith('complete'), result)
        self.assertEqual(sorted(DOC_POST_NUMBERS), sorted(d['post_number'] for d in docs))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        tree = ast.parse(code)
        actual = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                actual.setdefault(node.name, hashlib.sha256(ast.get_source_segment(code, node).encode()).hexdigest())
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual.get(name), name)


if __name__ == '__main__':
    unittest.main()
