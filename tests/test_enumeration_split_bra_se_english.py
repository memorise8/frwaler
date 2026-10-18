"""bra-se-english: SiteVision search paging (startAtHit offset, 10 per page; the page
after the last is an empty ol.sv-search-result), detail page used for the PDF link."""
import ast
import contextlib
import copy
import hashlib
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load, run_split

SITE = 'bra-se-english'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """Unchanged crawl() with an in-memory module-level _curl."""
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured, calls, listing_calls = [], [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', transport), patch('time.sleep', lambda *a: None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(2, len(listing_calls))
        return captured, calls

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['2031-01-02-synthetic-one', '2030-05-06-synthetic-two'],
                         [p['external_id'] for p in self.raw_papers])

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (self.SITE + '.py')).read_text()
        actual = {}
        for node in ast.parse(code).body:
            if isinstance(node, ast.FunctionDef):
                actual['module:' + node.name] = hashlib.sha256(ast.get_source_segment(code, node).encode()).hexdigest()
            if isinstance(node, ast.ClassDef):
                for m in node.body:
                    if isinstance(m, ast.FunctionDef):
                        actual[m.name] = hashlib.sha256(ast.get_source_segment(code, m).encode()).hexdigest()
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def _parser(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        return p, ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, parser = self._parser()
        url = build_url(p.listing['url'], dict(p.listing['params'], startAtHit=310))
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        error = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertEqual('listing_structure_missing', error.extra.get('parse_error'))
        self.assertFalse(error.extra.get('explicit_empty', False))

    def test_page_of_only_foreign_hits_is_not_an_end(self):
        _, parser = self._parser()
        body = ('<ol class="sv-search-result"><li class="sv-search-hit"><h3 class="bra-reports__content--title">'
                '<a href="/other/swedish-page">Svensk sida</a></h3></li></ol>')
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, body, 'https://bra.se/x?startAtHit=10'))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url)
        result, docs = run_split(self.SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual({'2031-01-02-synthetic-one', '2030-05-06-synthetic-two'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
