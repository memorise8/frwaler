"""L3b-20261018: enumeration split for fafo-no-en.

Four category listings (/en/publications/<category>?page=N; 40 or 60 rows; the page
shows the category total in ul.uk-list-emphasis, which stays on pages past the end).
Plan: date_range_split with explicit segments = the crawler's four categories.
Memory-only: no network or subprocess (the end-to-end run uses a temporary SQLite)."""
import contextlib
import copy
import io
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load

SITE = 'fafo-no-en'
CATEGORIES = ['fafo-reports', 'fafo-notes', 'briefs', 'other-fafo-publications']


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_raw():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


def route(url):
    """(category, page) of a listing URL in crawl() form (/<cat>/<N>) or plan form (?page=N)."""
    m = re.search(r'/en/publications/([^/?#]+)(?:/(\d+))?/?(?:\?|$)', url)
    if not m:
        return None, None
    page = m.group(2) or (parse_qs(urlsplit(url).query).get('page') or ['1'])[0]
    return m.group(1), int(page)


class FafoSplit(unittest.TestCase):
    def setUp(self):
        self.fx = fixture()
        self.plan = plan_raw()
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.no_db = patch('sqlite3.connect', denied)
        self.no_db.start()
        self.addCleanup(lambda: self.no_db.stop() if self.no_db else None)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def body(self, url, fx):
        if url in fx['details']:
            return fx['details'][url]
        cat, page = route(url)
        if cat in fx['listings']:
            return fx['listings'][cat] if page == 1 else fx['empty_pages'][cat]
        return None

    def native(self, fx=None, categories=None):
        fx = fx or self.fx
        crawler, module = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', lambda url, *a, **k: self.body(url, fx)), \
                patch.object(module, '_CATEGORIES', categories or CATEGORIES), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def adapter_parser(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        return p, adapter, ListingParser(p.listing['parser'], total_cfg=p.listing.get('total'), adapter=adapter)

    def pipeline(self, category, fx=None, redirect=False):
        fx = fx or self.fx
        p, adapter, parser = self.adapter_parser()
        url = build_url(p.listing['url'].replace('{segment}', category), {'page': 1})
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, fx['listings'][category], url))
        papers = []

        def fetch(u):
            self.assertIn(u, fx['details'])
            return Response(200, {'Content-Type': 'text/html'}, fx['details'][u], u + '?moved=1' if redirect else u)
        for item in page.items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return page, papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(categories=['fafo-reports'])
        self.assertEqual(['99001', '99000'], [p['external_id'] for p in expected])
        page, actual = self.pipeline('fafo-reports')
        self.assertEqual(2, page.total)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_all_categories_match_original_crawl(self):
        expected = self.native()
        actual = []
        for cat in CATEGORIES:
            actual += self.pipeline(cat)[1]
        self.assertEqual([clean_paper(p) for p in expected], actual)

    def test_short_abstract_skip_is_preserved(self):
        fx = json.loads(json.dumps(self.fx, ensure_ascii=False).replace(self.fx['long_text'], 'Short.'))
        self.assertEqual([], self.native(fx, categories=['fafo-reports']))
        self.assertEqual([], self.pipeline('fafo-reports', fx)[1])

    def test_detail_redirect_preserves_registered_identity(self):
        expected = self.native(categories=['fafo-reports'])
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline('fafo-reports', redirect=True)[1])

    def test_all_original_methods_are_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_empty_page_error_page_and_wrong_shape(self):
        p, adapter, parser = self.adapter_parser()
        url = 'https://www.fafo.no/en/publications/fafo-reports?page=36'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fx['empty_pages']['fafo-reports'], url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        error = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error.extra.get('parse_error'))
        self.assertFalse(error.extra.get('explicit_empty', False))
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items('<html></html>', 'https://www.fafo.no/en/unknown-category')

    def test_segments_are_the_crawler_categories(self):
        _, module = load(SITE)
        keys = [i['key'] for i in self.plan['sites'][SITE]['segments']['items']]
        self.assertEqual(list(module._CATEGORIES), keys)

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fx = self.fx
        plan = copy.deepcopy(self.plan['sites'][SITE])
        plan['request_interval_s'] = 0

        def handler(url):
            if urlsplit(url).path == '/robots.txt':
                return Response(404, {}, b'')
            body = self.body(url, fx)
            if body is None:
                return Response(404, {'Content-Type': 'text/html'}, '<html>404</html>', url)
            return Response(200, {'Content-Type': 'text/html'}, body, url)

        self.no_db.stop()
        self.no_db = None
        env = Env(SITE)
        try:
            crawler = type(load(SITE)[0])(env.conn)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=crawler, today='2026-10-18')
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual({'99000', '99001', '99002', '99003', '99004'}, {d['post_number'] for d in docs})


if __name__ == '__main__':
    unittest.main()
