"""NR-20261018: netl-doe-gov-advsearch enumeration split (crawl() unchanged, ext:pdf_text), memory-only.
The original crawl() runs with a fake transport and a fake _extract_pdf_text; the adapter pipeline
gets the same text through a fake crawler.enumeration.pdftext.extract. No network/subprocess/DB."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load, split_fixture

SITE = 'netl-doe-gov-advsearch'


class AdvsearchSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())

        def denied(*a, **k):
            raise AssertionError('attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, fixture):
        crawler, module = load(SITE)
        captured, lists = [], []

        def transport(url, **kw):
            lists.append(url)
            return (fixture['listing_body'] if len(lists) == 1 else fixture['empty_body']).encode()

        def pdf_text(url):
            text = fixture['pdf_texts'].get(url)
            return text.strip() if text else ''
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda p: captured.append(copy.deepcopy(p))
        with patch.object(module, '_curl_get', transport), patch.object(module, '_extract_pdf_text', pdf_text), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, fixture):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)).items
        self.assertEqual(2, len(items))  # the row without a PDF is never saved by crawl()

        def fetch(u):
            self.assertIn(u, fixture['pdf_texts'])
            return Response(200, {'Content-Type': 'application/pdf'}, b'%PDF-' + u.encode(), u)

        def extract(body, spec):
            self.assertEqual(['-layout'], spec['args'])
            return fixture['pdf_texts'][body[5:].decode()]
        out = []
        with patch('crawler.enumeration.pdftext.extract', extract):
            for it in items:
                paper = adapter.materialize(it, SimpleNamespace(get=fetch))
                if paper is not None:
                    out.append(paper)
        return out

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.fixture)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.fixture)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(8000, len(actual[1]['abstract']))

    def test_short_pdf_text_skip_is_preserved(self):
        f = copy.deepcopy(self.fixture)
        f['pdf_texts'] = {k: '  Short.\n' for k in f['pdf_texts']}
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_missing_pdf_text_is_skipped(self):
        crawler, _ = load(SITE)
        item = {'rel_href': '/node/1', 'pdf_url': 'https://netl.doe.gov/a.pdf', 'raw_date': '', 'title': 't',
                'detail_url': 'https://netl.doe.gov/node/1'}
        with guarded():
            self.assertIsNone(crawler._enumeration_paper(None, item))

    def test_empty_and_error_listing_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertTrue(page.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', url))
        self.assertTrue(err.extra.get('parse_error'))
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items({'error': 1})

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
