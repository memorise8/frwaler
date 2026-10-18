"""NR2-20261018: skm-dk-aktuelt enumeration split (crawl() unchanged), memory-only.
crawl() takes the two delivered scopes from svmn.dk/sitemap.xml; the plan uses the sitemap
strategy with the same scope filter and crawl()'s HTML __NEXT_DATA__ detail path."""
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
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture, split_plan

SITE = 'skm-dk-aktuelt'
SITEMAP = 'https://svmn.dk/sitemap.xml'


class RegisteredSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def native(self, fixture):
        crawler, module = load(SITE)
        captured = []
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def transport(url, *a, **k):
            if url == SITEMAP:
                return fixture['listing_body']
            return fixture['details'].get(url, fixture['empty_body'])  # listing page: no buildId
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured

    def pipeline(self, fixture, redirect=False):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        import re
        inc = [re.compile(x) for x in p.section('sitemap')['include']]
        locs = re.findall(r'<loc>([^<]+)</loc>', fixture['listing_body'])
        papers = []
        for loc in [l for l in locs if any(r.search(l) for r in inc)]:
            def fetch(url):
                return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url],
                                url + '?moved=1' if redirect else url)
            paper = adapter.materialize(Item(key=loc, url=loc, payload={'detail_url': loc}), SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.fixture)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.fixture)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_detail_redirect_preserves_identity(self):
        self.assertEqual([clean_paper(p) for p in self.native(self.fixture)], self.pipeline(self.fixture, redirect=True))

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'x'))
        f = json.loads(json.dumps(f, ensure_ascii=False).replace('Kort teaser.', ''))
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_scope_filter_matches_crawl(self):
        import re
        p = site_plan(self.plan, SITE, registered=[SITE])
        inc = [re.compile(x) for x in p.section('sitemap')['include']]
        crawler, module = load(SITE)
        with patch.object(module, '_curl_get', lambda url, *a, **k: self.fixture['listing_body']), \
                contextlib.redirect_stdout(io.StringIO()):
            native_urls = crawler._get_sitemap_urls()
        locs = re.findall(r'<loc>([^<]+)</loc>', self.fixture['listing_body'])
        self.assertEqual(native_urls, [l for l in locs if any(r.search(l) for r in inc)])

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0

        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url == SITEMAP:
                return Response(200, {'Content-Type': 'application/xml'}, fixture['listing_body'], url)
            if url in fixture['details']:
                return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
            return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
