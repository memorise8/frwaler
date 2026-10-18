"""kma-go-kr-kma: unfiltered press list (rows) + detail page (paper).
Memory-only comparison of _enumeration_items/_enumeration_detail with the unchanged crawl()."""
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
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture, split_plan

SITE = 'kma-go-kr-kma'


def native(fixture):
    crawler, module = load(SITE)
    captured, listing_calls = [], []

    def transport(url):
        if url in fixture['details']:
            return fixture['details'][url].encode()
        listing_calls.append(url)
        return (fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']).encode()

    crawler._delay = 0
    crawler._last_save_created = True
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
            contextlib.redirect_stdout(io.StringIO()):
        result = crawler.crawl(limit=10)
    assert result == len(captured)
    assert len(listing_calls) <= 2
    return captured


def parsed_plan():
    return site_plan({'version': 1, 'sites': {SITE: split_plan(SITE)}}, SITE, registered=[SITE])


def pipeline(fixture, page_url=None, redirect=False):
    plan = parsed_plan()
    crawler, _ = load(SITE)
    adapter = Adapter(SITE, plan.section('adapter'), None, crawler=crawler)
    url = page_url or build_url(plan.listing['url'], dict(plan.listing.get('params'), page=1))
    items = ListingParser(plan.listing['parser'], adapter=adapter).parse(
        Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)).items
    raw = []
    original = crawler._enumeration_detail

    def capture(*args, **kwargs):
        paper = original(*args, **kwargs)
        if paper is not None:
            raw.append(copy.deepcopy(paper))
        return paper
    crawler._enumeration_detail = capture

    def fetch(u):
        assert u in fixture['details'], 'undeclared detail request ' + u
        return Response(200, {'Content-Type': 'text/html'}, fixture['details'][u], u + '&moved=1' if redirect else u)
    papers = [p for p in (adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items) if p is not None]
    return items, papers, raw


class RegisteredSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = native(self.fixture)
        self.assertEqual(2, len(expected))
        items, actual, raw = pipeline(self.fixture)
        self.assertEqual(2, len(items))
        self.assertEqual(expected, raw)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(2, len({paper_identity(p) for p in actual}))
        # list-row PDF and detail-page PDF fallbacks are both exercised
        self.assertTrue(all(p['pdf_url'].endswith('.pdf') for p in expected))

    def test_bare_listing_url_means_page_one(self):
        plan = parsed_plan()
        bare = build_url(plan.listing['url'], plan.listing.get('params'))
        self.assertEqual([i.url for i in pipeline(self.fixture)[0]],
                         [i.url for i in pipeline(self.fixture, page_url=bare)[0]])

    def test_detail_url_carries_list_page_like_crawl(self):
        plan = parsed_plan()
        url = build_url(plan.listing['url'], dict(plan.listing.get('params'), page=3))
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'].encode(), url)
        self.assertTrue(all(r['url'].endswith('&page=3') for r in rows))

    def test_detail_redirect_preserves_registered_identity(self):
        expected = native(self.fixture)
        self.assertEqual([clean_paper(p) for p in expected], pipeline(self.fixture, redirect=True)[1])

    def test_short_abstract_falls_back_like_crawl(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected = native(f)
        self.assertEqual([clean_paper(p) for p in expected], pipeline(f)[1])

    def test_caller_input_is_not_mutated(self):
        before = copy.deepcopy(self.fixture)
        pipeline(self.fixture)
        self.assertEqual(before, self.fixture)

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        plan = parsed_plan()
        crawler, _ = load(SITE)
        parser = ListingParser(plan.listing['parser'], adapter=Adapter(SITE, plan.section('adapter'), None, crawler=crawler))
        url = build_url(plan.listing['url'], dict(plan.listing.get('params'), page=7))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(unittest.TestCase):
    def setUp(self):
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)
        self.fixture = split_fixture(SITE)

    def end(self, url):
        return Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url)

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=self.end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_maintenance_listing_ends_incomplete(self):
        result, docs = run_split(SITE, end=self.end, listing='<html><body><p>점검 중</p></body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
