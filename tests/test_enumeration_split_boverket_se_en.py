"""boverket-se-en: the publications RSS feed (one page; ?page=N repeats it, a wrap end),
detail pages converted by the pure copy of _fetch_and_save."""
import ast
import hashlib
import unittest

from crawler.enumeration.http import Response
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, run_split

SITE = 'boverket-se-en'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['990001', '990002'], [p['external_id'] for p in self.raw_papers])

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


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def _end(self, body):
        return lambda url: Response(200, {'Content-Type': self.fixture['content_type']}, body, url)

    def test_shipped_plan_completes_end_to_end(self):
        # The live feed answers every ?page=N with the same single page.
        for name, body in (('wrap', self.fixture['listing_body']), ('empty_channel', '<rss><channel/></rss>')):
            with self.subTest(end=name):
                result, docs = run_split(self.SITE, end=self._end(body))
                self.assertEqual('complete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
                self.assertEqual({'990001', '990002'}, {d['post_number'] for d in docs})
                self.assertEqual(2, len({d['meta_url'] for d in docs}))
