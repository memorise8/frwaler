"""L3b-20261018: enumeration split for epa-ie-news-releases.

One page per year (/news-releases/news-releases-YYYY/, all releases of the year;
?page/?year are ignored by the site, so page 2 repeats page 1 and ends the year).
Plan: date_range_split, yearly segments from 2023 (no splitting). Memory-only:
no network, subprocess or real database."""
import contextlib
import copy
import io
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load

SITE = 'epa-ie-news-releases'


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_raw():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


def year_of(url):
    m = re.search(r'news-releases-(\d{4})/', url)
    return m.group(1) if m else None


class EpaSplit(unittest.TestCase):
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

    def native(self, fx=None):
        fx = fx or self.fx
        crawler, _ = load(SITE)
        captured = []

        def transport(url, *a, **k):
            if url in fx['details']:
                return fx['details'][url]
            return fx['listings'].get(year_of(url) or '', '<html><main/></html>')
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, year, fx=None, redirect=False):
        fx = fx or self.fx
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        url = build_url(p.listing['url'].replace('{segment}', year), {'year': year, 'page': 1})
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, fx['listings'][year], url))
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
        expected = self.native()
        self.assertEqual(['990002', '990001'], [p['external_id'] for p in expected])
        _, actual = self.pipeline('2025')
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        fx = json.loads(json.dumps(self.fx, ensure_ascii=False).replace(self.fx['long_text'], 'Short.'))
        # The list teaser ("Short synthetic teaser N.") is the fallback; both are < 50 chars.
        self.assertEqual([], self.native(fx))
        self.assertEqual([], self.pipeline('2025', fx)[1])

    def test_detail_redirect_preserves_registered_identity(self):
        expected = self.native()
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline('2025', redirect=True)[1])

    def test_all_original_methods_are_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_wrong_shape_and_error_page_are_listing_errors(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                crawler._enumeration_items({'error': 'not a listing'}, 'https://www.epa.ie/x')
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))
        p = site_plan(self.plan, SITE, registered=[SITE])
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>',
                                     'https://www.epa.ie/news-releases/news-releases-2025/?year=2025&page=1'))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('parse_error'))
        self.assertFalse(page.extra.get('explicit_empty', False))

    def test_year_segments_cover_the_year_pages(self):
        from crawler.enumeration.strategies import generate_segments
        from datetime import date
        p = site_plan(self.plan, SITE, registered=[SITE])
        self.assertEqual(['2023', '2024', '2025', '2026'], generate_segments(p.section('segments'), date(2026, 10, 18)))

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fx = self.fx
        plan = copy.deepcopy(self.plan['sites'][SITE])
        plan['request_interval_s'] = 0
        seen = []

        def handler(url):
            seen.append(url)
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fx['details']:
                return Response(200, {'Content-Type': 'text/html'}, fx['details'][url], url)
            year = year_of(url)
            if year in fx['listings']:
                return Response(200, {'Content-Type': 'text/html'}, fx['listings'][year], url)
            return Response(404, {'Content-Type': 'text/html'}, '<html>404</html>', url)

        # Only this run uses the temporary SQLite of tests.enumeration_fakes.Env;
        # network and subprocess stay denied.
        self.no_db.stop()
        self.no_db = None
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            crawler = cls(env.conn)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=crawler, today='2026-10-18')
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(5, len(docs))
        self.assertEqual({'989999', '990000', '990001', '990002', '990003'}, {d['post_number'] for d in docs})
        self.assertTrue(any('news-releases-2023/?year=2023&page=1' in u for u in seen))


if __name__ == '__main__':
    unittest.main()
