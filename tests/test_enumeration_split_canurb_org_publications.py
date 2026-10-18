"""L3b-20261018 (ext:path_paging): enumeration split for canurb-org-publications.
/publications/ holds the whole FacetWP catalogue on one page; /publications/page/N/ repeats it
(plan listing.url {page} + first_page_url: the framework reads page 2 as a wrap -> last page 1).
Detail cards are read from their detail page; direct-PDF and external-publisher cards are saved
from the card (crawl() requests nothing for them; the plan points them at the listing page,
whose body is ignored). external_id = post id else slug, post_number = post id (unchanged).
crawl() is unchanged; the reference is _crawl_listing()'s own save argument."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'canurb-org-publications'
LIST = 'https://canurb.org/publications/'


def html_resp(body, url, status=200):
    return Response(status, {'Content-Type': 'text/html; charset=UTF-8'}, body, url)


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
        """crawl()'s listing path (_crawl_listing) fed from the fixture."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, calls = [], []

        def curl(url, context='request'):
            calls.append(url)
            if url == LIST:
                return fixture['listing_body']
            return fixture['details'][url]

        def save(paper):
            captured.append(copy.deepcopy(paper))
            return len(captured)
        crawler._curl_get = curl
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = save
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler._crawl_listing(limit=10)
        self.assertEqual([LIST, LIST + 'synthetic-report/'], calls)  # no request for linked cards
        return captured

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(html_resp(fixture['listing_body'], LIST)).items
        self.assertEqual(3, len(items))
        papers = []

        def get(u):
            self.assertIn(u, fixture['details'], 'undeclared detail request')
            return html_resp(fixture['details'][u], u)
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=get))
            if paper is not None:
                papers.append(paper)
        return papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['90001', '90002', '90003'], [p['external_id'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False)
                       .replace(self.fixture['long_text'], 'Short.')
                       .replace('Synthetic social description.', 'Short.')
                       .replace(self.fixture['card_excerpt'], 'Short.'))
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_page_without_cards_is_a_listing_parse_error(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(html_resp('<html><body><p>Maintenance</p></body></html>', LIST))
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
    """The shipped plan through the real SiteRunner: /page/2/ repeats the catalogue (wrap)."""

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)

        def handler(url):
            path = urlsplit(url).path
            if path == '/robots.txt':
                return Response(404, {}, b'', url)
            if url in fixture['details']:
                return html_resp(fixture['details'][url], url)
            if path.startswith('/publications/page/'):
                return html_resp(fixture['listing_body'], url)
            return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['90001', '90002', '90003'], sorted(d['post_number'] for d in docs))


if __name__ == '__main__':
    unittest.main()
