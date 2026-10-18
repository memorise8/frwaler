"""cancer-fr-presse: last_page_backward plan + pure list/detail conversions (memory only).

The press list (``?page=N``, 10 rows, the site re-serves the last page past the end)
is read by ``_enumeration_items(html)``; each detail page is converted by
``_enumeration_paper(html, url)``, which must equal what the unchanged crawl() saves.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from tests.collector_split_helpers import ROOT, load, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'cancer-fr-presse'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """Original crawl() with its own ``_curl`` transport replaced by memory pages."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, listing_calls = [], [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_module_helpers_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['module_function_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_identity_is_the_registered_slug(self):
        expected, _ = self.native()
        self.assertEqual(['offline-1', 'offline-2'], [p['external_id'] for p in expected])
        self.assertEqual(['offline-1', 'offline-2'], [p['external_id'] for p in self.pipeline()])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def test_clamped_last_page_completes(self):
        """Past the last page the live site re-serves the last page; that ends the listing."""
        body = self.fixture['listing_body']
        result, docs = run_split(self.SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
