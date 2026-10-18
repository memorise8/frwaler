"""NR-20261018: justitsministeriet-dk-forskning enumeration split (crawl() unchanged), memory-only.
Year-range sub-pages are discovered on the hub page (ext:multi_listing discover); each is one
single-page segment. Papers come from the listing rows only (paper_method)."""
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
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture

SITE = 'justitsministeriet-dk-forskning'
HUB = 'https://www.justitsministeriet.dk/forskning/forskningsrapporter/'
YEAR = 'https://www.justitsministeriet.dk/forskning/forskningsrapporter/forskningsrapporter-2023-2026/'


def no_io(test):
    def denied(*a, **k):
        raise AssertionError('attempted network/process/database I/O')
    for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
        p = patch(name, denied); p.start(); test.addCleanup(p.stop)


class Split(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        no_io(self)

    def native(self, fixture):
        crawler, module = load(SITE)
        captured = []
        pages = {HUB: fixture['listing_body'], YEAR: fixture['listing_body']}
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', lambda url, *a, **k: pages.get(url, '')), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, fixture):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        resp = Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], YEAR + '?page=1')
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(resp).items
        self.assertEqual(2, len(items))
        fetch = SimpleNamespace(get=lambda url: self.fail('paper_method must not fetch'))
        return [x for x in (adapter.materialize(i, fetch) for i in items) if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.fixture)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.fixture)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(2, len({paper_identity(p) for p in actual}))

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Kort.'))
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_wrong_shape_and_error_page(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items({'error': 1}, YEAR)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], YEAR))
        self.assertTrue(empty.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', YEAR))
        self.assertTrue(err.extra.get('parse_error'))


class SplitRun(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def test_shipped_plan_completes_end_to_end(self):
        # Hub and year page answer with the fixture; ?page=2 repeats page 1 (single-page segment).
        body = split_fixture(SITE)['listing_body']
        from tests.collector_split_helpers import split_plan
        plan = split_plan(SITE)
        plan['segments']['min_segments'] = 1  # the fixture hub lists one of the six year pages
        result, docs = run_split(SITE, plan=plan, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertIn(result['status'], ('complete', 'complete_unverified'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
