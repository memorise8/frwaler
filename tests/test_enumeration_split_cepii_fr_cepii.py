"""cepii-fr-cepii: last_page_backward plan over ``reports.asp?offset=N`` (offset mode,
10 rows per page; past the end the site re-serves the last page) + pure conversions.

``_enumeration_items(html)`` reuses the registered list parser and
``_enumeration_paper(html, entry)`` is a pure copy of crawl()'s per-entry conversion after
the detail fetch; both must give what the unchanged crawl() saves (memory only).
"""
import ast
import hashlib
import unittest

from crawler.enumeration.http import Response
from tests.collector_split_helpers import ROOT, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'cepii-fr-cepii'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_module_helpers_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['module_function_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_identity_and_date_precision_are_preserved(self):
        expected, _ = self.native()
        actual = self.pipeline()
        self.assertEqual(['91', '92'], [p['external_id'] for p in actual])
        # A month-only citation keeps published_date empty, exactly as crawl() does.
        self.assertEqual(['2026-03-12', None], [p['published_date'] for p in expected])
        self.assertEqual([p['published_date'] for p in expected], [p['published_date'] for p in actual])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def test_clamped_last_page_completes(self):
        body = self.fixture['listing_body']
        result, docs = run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
