"""issnationallab-org-research-and-science: WP child pages (list) + detail HTML (paper).
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
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan
from tests.issnationallab_split_helpers import WpRunChecks

SITE = 'issnationallab-org-research-and-science'


def native(fixture):
    """Run the unchanged crawl() with an in-memory session; return saved papers."""
    crawler, _ = load(SITE)
    captured, listing_calls = [], []

    def get(url, params=None, **kwargs):
        if url in fixture['details']:
            body = fixture['details'][url]
            return SimpleNamespace(status_code=200, text=body, raise_for_status=lambda: None,
                                   json=lambda: json.loads(body))
        if '/wp-json/wp/v2/dlm_download/' in url:
            raise AssertionError('fixture must not need the DLM fallback')
        listing_calls.append(url)
        body = fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        return SimpleNamespace(status_code=200, text=body, raise_for_status=lambda: None,
                               json=lambda: json.loads(body))

    crawler._session = SimpleNamespace(get=get)
    crawler._last_save_created = True
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
        result = crawler.crawl(limit=10)
    assert result == len(captured)
    return captured


def pipeline(fixture, redirect=False):
    plan = site_plan({'version': 1, 'sites': {SITE: split_plan(SITE)}}, SITE, registered=[SITE])
    crawler, _ = load(SITE)
    adapter = Adapter(SITE, plan.section('adapter'), None, crawler=crawler)
    url = build_url(plan.listing['url'], plan.listing.get('params'))
    items = ListingParser(plan.listing['parser'], adapter=adapter).parse(
        Response(200, {'Content-Type': 'application/json'}, fixture['listing_body'], url)).items
    raw = []
    original = crawler._enumeration_detail

    def capture(*args, **kwargs):
        paper = original(*args, **kwargs)
        if paper is not None:
            raw.append(copy.deepcopy(paper))
        return paper
    crawler._enumeration_detail = capture

    def fetch(u):
        assert u in fixture['details'], 'undeclared detail request'
        return Response(200, {'Content-Type': 'text/html'}, fixture['details'][u], u + '?moved=1' if redirect else u)
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

    def test_detail_redirect_preserves_registered_identity(self):
        expected = native(self.fixture)
        self.assertEqual([clean_paper(p) for p in expected], pipeline(self.fixture, redirect=True)[1])

    def test_short_abstract_without_download_is_skipped_like_crawl(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        for url, body in f['details'].items():
            f['details'][url] = body.replace('/download/', '/files/')
        self.assertEqual([], native(f))
        self.assertEqual([], pipeline(f)[1])

    def test_short_abstract_needing_dlm_fallback_fails_loudly(self):
        """crawl() would ask the DLM REST API (a second request); the pure method refuses
        instead of silently skipping the report."""
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        crawler, _ = load(SITE)
        row = {'record': json.loads(f['listing_body'])[0]}
        html = f['details'][row['record']['link']]
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_detail(html, row)
        self.assertEqual('detail_needs_dlm_fallback', ctx.exception.reason)

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

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(SITE)
        for bad in ({'error': 'not a listing'}, [{'id': True, 'link': 'x', 'title': {}}], [{'id': 1, 'title': {}}]):
            with self.assertRaises(PermanentFailure) as ctx:
                crawler._enumeration_items(bad)
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        plan = site_plan({'version': 1, 'sites': {SITE: split_plan(SITE)}}, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(plan.listing['parser'], adapter=Adapter(SITE, plan.section('adapter'), None, crawler=crawler))
        url = build_url(plan.listing['url'], plan.listing.get('params'))
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, '[]', url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"error":"maintenance"}', url))


class RegisteredSplitRun(WpRunChecks, unittest.TestCase):
    SITE = SITE

    def short_fixture(self):
        # Without a download link, a short page is a legitimate crawl() skip.
        f = super().short_fixture()
        for url, body in f['details'].items():
            f['details'][url] = body.replace('/download/', '/files/')
        return f


if __name__ == '__main__':
    unittest.main()
