"""amf-france-org-fr: sitemap urlset read as a one-page listing (``?page=2`` is 404),
detail pages converted by the pure copy of crawl()'s per-item code."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load, run_split

SITE = 'amf-france-org-fr'
NOT_FOUND = lambda url: Response(404, {'Content-Type': 'text/html'}, '<html><body>Not found</body></html>', url)


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """Unchanged crawl() with an in-memory module-level _curl_get ((text, headers) tuple)."""
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured, calls = [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url], {}
            self.assertEqual(crawler.SITEMAP_URL, url)
            return fixture['listing_body'], {}
        crawler._last_save_created = True
        crawler._report_source_failure = lambda reason: self.fail('source failure: ' + reason)
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None):
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        # crawl() saves newest lastmod first; the adapter keeps sitemap order.
        captured.sort(key=lambda p: p['url'])
        return captured, calls

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['90001', '90002'], sorted(p['post_number'] for p in self.raw_papers))

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

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """The real end is the 404 for ?page=2 (run test below). A 200 sitemap without
        communiques is not proof of an empty source, and an HTML page is a parse error."""
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = build_url(p.listing['url'], {'page': 1})
        empty = parser.parse(Response(200, {'Content-Type': 'text/xml'},
                                      '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>', url))
        self.assertEqual([], empty.items)
        self.assertFalse(empty.extra.get('explicit_empty', False))
        for body in ('<html>maintenance</html>', '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>'):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_listing_keeps_only_communiques_with_lastmod(self):
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'], 'https://www.amf-france.org/sitemap.xml')
        self.assertEqual(['2026-10-01', '2026-09-15'], [r['lastmod'] for r in rows])
        self.assertTrue(all(crawler.TARGET_PATH_PREFIX in r['url'] for r in rows))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(self.SITE, end=NOT_FOUND)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'90001', '90002'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_empty_sitemap_does_not_complete(self):
        result, docs = run_split(self.SITE, end=NOT_FOUND,
                                 listing='<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"/>')
        self.assertNotEqual('complete', result['status'], result)
        self.assertEqual([], docs)
