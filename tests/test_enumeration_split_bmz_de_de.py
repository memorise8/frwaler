"""L3b-20261018 (ext:http204_end): enumeration split for bmz-de-de.
GET /ajax/filterlist/de/24710-24710?offset=N&limit=9 (offset, 9 rows); past the last page
the source answers 204 No Content (plan listing.end_on_204). Detail
/de/aktuelles/publikationen/{id}-{id}; post_number = content id (unchanged). crawl() is
unchanged; the reference is crawl()'s own save argument."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'bmz-de-de'
LIST = 'https://www.bmz.de/ajax/filterlist/de/24710-24710'


def html_resp(body, url, status=200):
    return Response(status, {'Content-Type': 'text/html;charset=UTF-8'}, body, url)


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
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, lists = [], []

        def curl(url, *, context='', timeout=45):
            if url in fixture['details']:
                return fixture['details'][url]
            lists.append(url)
            return fixture['listing_body']
        crawler._curl_get = curl
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        self.assertEqual([LIST + '?offset=0&limit=9'], lists)  # data-hits=2 reached
        return captured

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            html_resp(fixture['listing_body'], LIST + '?limit=9&offset=0')).items
        self.assertEqual(2, len(items))
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
        self.assertEqual(['900002', '900001'], [p['post_number'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Kurz.'))
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_list_page_and_offset_come_from_the_url(self):
        _, adapter = self.adapter()
        crawler = adapter.crawler
        rows = crawler._enumeration_items(self.fixture['listing_body'], LIST + '?limit=9&offset=81')
        self.assertEqual({(10, 81)}, {(r['list_page'], r['list_offset']) for r in rows})

    def test_empty_and_error_pages(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = LIST + '?limit=9&offset=90'
        page = parser.parse(html_resp('<div class="m-publications"><div data-hits="84"></div><ul></ul></div>', url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(html_resp('<html><body><p>Wartungsarbeiten</p></body></html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))
        self.assertIs(True, p.listing.get('end_on_204'))

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
    """The shipped plan through the real SiteRunner; the page after the last one is 204."""

    def run_plan(self, end_body=b''):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'', url)
            if url in fixture['details']:
                return html_resp(fixture['details'][url], url)
            if parts.path == urlsplit(LIST).path:
                offset = int(parse_qs(parts.query).get('offset', ['0'])[0])
                return html_resp(fixture['listing_body'], url) if offset == 0 else Response(204, {}, end_body, url)
            return Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            return result, env.docs()
        finally:
            env.close()

    def test_shipped_plan_completes_on_204(self):
        result, docs = self.run_plan()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['900001', '900002'], sorted(d['post_number'] for d in docs))

    def test_204_with_a_body_is_not_completion(self):
        result, _ = self.run_plan(b'oops')
        self.assertNotEqual('complete', result['status'], result)


if __name__ == '__main__':
    unittest.main()
