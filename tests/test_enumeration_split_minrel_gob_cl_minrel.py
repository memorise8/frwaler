"""L3b-20261018 (ext:path_paging): enumeration split for minrel-gob-cl-minrel.

The press room pages by path (/minrel/sala-de-prensa/p/{N}, 51 cards); a page number past
the end repeats the last page (clamp). Cards are parsed with _parse_list; the detail
conversion is crawl()'s, copied verbatim (external_id = '<14-digit stamp>:<URL path>').
Memory only: no network, subprocess or database."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'minrel-gob-cl-minrel'
ROOT = Path(__file__).resolve().parents[1]
FIRST = 'https://www.minrel.gob.cl/minrel/sala-de-prensa/p/1'


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_doc():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class Split(unittest.TestCase):
    def setUp(self):
        self.fx = fixture()
        self.plan = site_plan(plan_doc(), SITE, registered=[SITE])
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured, lists = [], []
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def get(url, context='', referer=None, accept=None):
            if url in self.fx['details']:
                return self.fx['details'][url]
            lists.append(url)
            return self.fx['listing_body']
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual([FIRST], lists)  # no next link in the fixture: one list page
        self.assertEqual(len(captured), result)
        return captured

    def adapter(self):
        crawler, _ = load(SITE)
        return Adapter(SITE, self.plan.section('adapter'), None, crawler=crawler)

    def pipeline(self, redirect=False):
        adapter = self.adapter()
        resp = Response(200, {'Content-Type': 'text/html'}, self.fx['listing_body'], FIRST)
        items = ListingParser(self.plan.listing['parser'], adapter=adapter).parse(resp).items
        self.assertEqual(3, len(items))

        def get(url):
            return Response(200, {'Content-Type': 'text/html'}, self.fx['details'][url],
                            url + '?moved=1' if redirect else url)
        return [p for p in (adapter.materialize(i, SimpleNamespace(get=get)) for i in items) if p is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['20311017100000:sala-de-prensa/synthetic-one', 'sala-de-prensa/synthetic-two'],
                         [p['external_id'] for p in expected])
        for redirect in (False, True):
            actual = self.pipeline(redirect)
            self.assertEqual([clean_paper(p) for p in expected], actual)
            self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_page_url_is_a_path_and_the_first_page_is_p1(self):
        from crawler.enumeration.strategies import Strategy
        strat = Strategy(SimpleNamespace(plan=self.plan, fetcher=None, parser=None, store=None, site_id=SITE))
        self.assertEqual(FIRST, strat.page_url(1))
        self.assertEqual('https://www.minrel.gob.cl/minrel/sala-de-prensa/p/94', strat.page_url(94))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_shipped_plan_completes_with_clamped_last_page(self):
        from tests.enumeration_fakes import Env
        site = copy.deepcopy(plan_doc()['sites'][SITE])
        site['request_interval_s'] = 0

        def handle(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'', url)
            if url in self.fx['details']:
                return Response(200, {'Content-Type': 'text/html'}, self.fx['details'][url], url)
            self.assertIn('/minrel/sala-de-prensa/p/', url)
            return Response(200, {'Content-Type': 'text/html'}, self.fx['listing_body'], url)  # every page clamps to p/1
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, handle, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        # Completed through the clamp end; the short-abstract card crawl() skips is ledgered for review.
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual((2, 1), (result['stats']['saved_new'], result['stats']['failed_permanent']))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertFalse([u for u in result['transport'].urls if '?' in u and 'page' in u])


if __name__ == '__main__':
    unittest.main()
