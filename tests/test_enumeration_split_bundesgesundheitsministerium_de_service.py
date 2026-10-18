"""L3b-20261018 (ext:path_paging): enumeration split for bundesgesundheitsministerium-de-service.
List /service/publikationen/gesundheit (first page) and /seite-N (plan listing.url {page} +
first_page_url); every page repeats the pinned div.c-teaser--top card, which crawl() saves
once (first page). Past the last page: /seite-(L+1) repeats page L (clamp), far past it 404.
external_id = slug, post_number = Artikel-Nr. else slug (unchanged). crawl() is unchanged."""
import contextlib
import copy
import io
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'bundesgesundheitsministerium-de-service'
FIRST = 'https://www.bundesgesundheitsministerium.de/service/publikationen/gesundheit'


def html_resp(body, url, status=200):
    return Response(status, {'Content-Type': 'text/html; charset=utf-8'}, body, url)


class Split(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {'sites': {SITE: split_plan(SITE)}}
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        """crawl() fed from the fixture: first page, then /seite-2 (pinned + one new card),
        then /seite-3 repeating /seite-2 (all seen: crawl() stops)."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, lists = [], []

        def curl(url, *, referer=None, timeout=45):
            if url in fixture['details']:
                return fixture['details'][url]
            lists.append(url)
            return fixture['listing_body'] if url == FIRST else fixture['listing_page2_body']
        crawler._curl = curl
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        self.assertEqual([FIRST, FIRST + '/seite-2', FIRST + '/seite-3'], lists)
        return captured

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        items = parser.parse(html_resp(fixture['listing_body'], FIRST)).items
        items += parser.parse(html_resp(fixture['listing_page2_body'], FIRST + '/seite-2')).items
        papers = []

        def get(u):
            self.assertIn(u, fixture['details'], 'undeclared detail request')
            return html_resp(fixture['details'][u], u)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=get))
            if paper is not None:
                papers.append(paper)
        return items, papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['synthetic-pinned', 'synthetic-alpha', 'synthetic-beta', 'synthetic-gamma'],
                         [p['external_id'] for p in expected])
        self.assertEqual(['BMG-X-9', 'BMG-X-1', 'synthetic-beta', 'BMG-X-3'], [p['post_number'] for p in expected])
        items, actual = self.pipeline()
        self.assertEqual(4, len(items))  # the pinned card only once (first page)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Kurz.'))
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f)[1])

    def test_page_urls_put_the_page_in_the_path(self):
        from crawler.enumeration.strategies import Strategy
        p = site_plan(self.plan, SITE, registered=[SITE])
        strat = Strategy(SimpleNamespace(plan=p, fetcher=None, parser=None, store=None, site_id=SITE))
        self.assertEqual(FIRST, strat.page_url(1))
        self.assertEqual(FIRST + '/seite-2', strat.page_url(2))
        self.assertEqual(FIRST + '/seite-52', strat.page_url(52))

    def test_error_page_is_a_listing_parse_error(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(html_resp('<html><body><p>Wartung</p></body></html>', FIRST + '/seite-9'))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_original_methods_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


class SplitRun(unittest.TestCase):
    """The shipped plan through the real SiteRunner: two pages, then 404."""

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)
        requested = []

        def handler(url):
            requested.append(url)
            path = urlsplit(url).path
            if path == '/robots.txt':
                return Response(404, {}, b'', url)
            if url in fixture['details']:
                return html_resp(fixture['details'][url], url)
            if url == FIRST:
                return html_resp(fixture['listing_body'], url)
            m = re.search(r'/seite-(\d+)$', path)
            if m and m.group(1) == '2':
                return html_resp(fixture['listing_page2_body'], url)
            return html_resp('<html><body>Nicht gefunden</body></html>', url, 404)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['synthetic-alpha', 'synthetic-beta', 'synthetic-gamma', 'synthetic-pinned'],
                         sorted(d['post_number'] for d in docs))
        self.assertFalse([u for u in requested if 'page=' in u or u.endswith('/seite-1')])


if __name__ == '__main__':
    unittest.main()
