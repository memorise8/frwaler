"""NR2-20261018: pbocri-org-cn-lnzy3html enumeration split (crawl() unchanged), memory-only.
The PDF text comes from the framework's pdftotext (X2 pdf_text, `-enc UTF-8` like crawl());
here that text is given directly, so no subprocess runs."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'pbocri-org-cn-lnzy3html'
LIST = 'https://www.pbocri.org.cn/lnzy3.html'


class RegisteredSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)

    def native(self, text):
        crawler, module = load(SITE)
        captured = []
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', lambda url, *a, **k: self.fixture['listing_body'].encode()), \
                patch.object(type(crawler), '_fetch_abstract',
                             lambda self_, title, pdf_url: module._clean_text(text)[:3000]), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured

    def split(self, text):
        crawler, _ = load(SITE)
        with guarded():
            rows = crawler._enumeration_items(self.fixture['listing_body'].encode(), LIST)
            return [p for p in (crawler._enumeration_paper(text, b'%PDF' + b'0' * 200, r) for r in rows) if p]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.fixture['long_text'])
        actual = self.split(self.fixture['long_text'])
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, actual)
        self.assertEqual([paper_identity(clean_paper(p)) for p in expected],
                         [paper_identity(clean_paper(p)) for p in actual])

    def test_short_text_skip_is_preserved(self):
        self.assertEqual([], self.native('short'))
        self.assertEqual([], self.split('short'))

    def test_wrong_shape_is_a_listing_parse_error(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'error': 1}, LIST)

    def test_plan_uses_crawl_pdftotext_options(self):
        self.assertEqual(['-enc', 'UTF-8'], split_plan(SITE)['adapter']['pdf_text']['args'])

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
