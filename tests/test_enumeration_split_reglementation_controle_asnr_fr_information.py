"""reglementation-controle-asnr-fr-information: rapports d'expertise list (?page=N,
page past the last is 404), detail pages converted by the pure copy of crawl()."""
import ast
import hashlib
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load, run_split

SITE = 'reglementation-controle-asnr-fr-information'
NOT_FOUND = lambda url: Response(404, {'Content-Type': 'text/html'}, '<html><body>Not found</body></html>', url)


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        # content/download id when a PDF exists, otherwise the slug (registered rule).
        self.assertEqual(['990001', 'synthetic-two'], [p['post_number'] for p in self.raw_papers])

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
        """The end is the 404 after the last page (run test below); a 200 page
        without teasers is never an empty end page."""
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = build_url(p.listing['url'], {'page': 2})
        for body in ('<html><main/></html>', '<html>maintenance</html>'):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(self.SITE, end=NOT_FOUND)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual({'990001', 'synthetic-two'}, {d['post_number'] for d in docs})
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
