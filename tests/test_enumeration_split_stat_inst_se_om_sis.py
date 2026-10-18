"""stat-inst-se-om-sis: single remissvar page of PDF links; abstract, date and Dnr come from
the PDF text (ext:pdf_text: the framework runs pdftotext with crawl()'s options, none).
crawl() (unchanged) and the shipped plan are compared on synthetic data with a fake
pdftotext; one extra check runs the real tool on a generated PDF for both paths."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.test_enumeration_extensions_x2 import make_pdf

SITE = 'stat-inst-se-om-sis'
LIST_URL = 'https://www.stat-inst.se/om-sis/remissvar/'


def fake_framework_pdftotext(fixture):
    """crawler.enumeration.pdftext.extract stand-in: stdout for the fixture body, unstripped."""
    by_body = {fixture['details'][u].encode('utf-8'): t for u, t in fixture['pdf_texts'].items()}

    def extract(body, spec):
        if not body or len(body) < spec.get('min_bytes', 0):
            return None
        return by_body[bytes(body)]
    return extract


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with a memory _curl_get and a fake pdftotext subprocess."""
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        captured, calls = [], []
        by_body = {fixture['details'][u].encode('utf-8'): t for u, t in fixture['pdf_texts'].items()}

        def get(url, binary=False, timeout=60):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url].encode('utf-8') if binary else fixture['details'][url]
            return fixture['listing_body']

        def run(cmd, **kwargs):
            self.assertEqual('pdftotext', cmd[0])
            self.assertEqual(['-'], cmd[2:])  # no options, like the plan
            with open(cmd[1], 'rb') as fh:
                body = fh.read()
            return subprocess.CompletedProcess(cmd, 0, by_body[body].encode('utf-8'), b'')
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        with patch.object(module.subprocess, 'run', run), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def pipeline(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        with patch('crawler.enumeration.pdftext.extract', fake_framework_pdftotext(fixture)):
            return super().pipeline(fixture, redirect)

    def test_plan_pdf_text_matches_crawl_options(self):
        site = self.plan['sites'][SITE]
        self.assertEqual({'args': [], 'timeout_s': 30, 'min_bytes': 100}, site['adapter']['pdf_text'])
        self.assertEqual(['pdf_text', 'item'], site['adapter']['detail_args'])

    def test_fields_from_pdf_text(self):
        expected, _ = self.native()
        actual = self.pipeline()
        self.assertEqual('2031-03-05', actual[0]['published_date'])  # date in the PDF text
        self.assertEqual('2030-01-01', actual[1]['published_date'])  # no date: listing year
        self.assertEqual('1.2.3-456/2031', json.loads(actual[0]['metadata'])['dnr'])
        self.assertEqual('Remissvar & synpunkter SOU 2031:1 (pdf, nytt fönster)', actual[0]['title'])  # crawl() strips only '(, nytt fönster)'
        self.assertEqual('Remissvar Ds 2030:9', actual[1]['title'])
        self.assertEqual([clean_paper(p) for p in expected], actual)

    def test_identity_is_pdf_filename_and_meta_url_is_the_listing(self):
        expected, _ = self.native()
        actual = self.pipeline()
        self.assertEqual(['remissvar-syntetisk-sou-2031_1', 'remissvar-syntetisk-ds-2030_9'],
                         [paper_identity(p)[0] for p in actual])
        # Every paper's meta_url is the listing URL, exactly as crawl() stores it; the dedup
        # key (site_id, post_number, meta_url) still separates documents by post_number.
        self.assertEqual({LIST_URL}, {paper_identity(p)[1] for p in actual})
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_failed_or_short_pdf_text_is_skipped(self):
        crawler, _ = load(SITE)
        row = {'url': 'https://www.stat-inst.se/globalassets/a.pdf', 'raw_title': 'T', 'year': '2031'}
        self.assertIsNone(crawler._enumeration_paper(None, row))  # tool failed / file < 100 bytes
        self.assertIsNone(crawler._enumeration_paper('  \n\f', row))  # scanned PDF: no text layer
        self.assertIsNone(crawler._enumeration_paper(' short text \n', row))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """Single listing page: no empty end page (the end is the repeated page); a page
        without list__item PDF links is a listing error, never an empty end."""
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], p.listing['url']))
        self.assertEqual(2, len(page.items))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', p.listing['url']))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


@unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext not installed')
class RealPdftotext(unittest.TestCase):
    def test_framework_text_equals_crawl_text(self):
        """crawl()'s _extract_pdf_text() and the framework's extract() + the method's strip give
        the same string for a real PDF (both run the same pdftotext without options)."""
        from crawler.enumeration.pdftext import extract
        crawler, _ = load(SITE)
        body = make_pdf('Remissvar 2031-03-05 Dnr 1.2.3-456/2031 synthetic text long enough for the threshold')
        crawler._curl_get = lambda url, binary=False, timeout=60: body
        expected = crawler._extract_pdf_text('https://www.stat-inst.se/globalassets/a.pdf')
        text = extract(body, {'args': [], 'timeout_s': 30, 'min_bytes': 100})
        self.assertEqual(expected, text.strip())
        row = {'url': 'https://www.stat-inst.se/globalassets/a.pdf', 'raw_title': 'T', 'year': '2031'}
        paper = crawler._enumeration_paper(text, row)
        self.assertEqual(expected[:3000].strip(), paper['abstract'])
        self.assertEqual('2031-03-05', paper['published_date'])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        p = patch('crawler.enumeration.pdftext.extract', fake_framework_pdftotext(self.fixture))
        p.start()
        self.addCleanup(p.stop)

    def repeat(self, fixture=None):
        f = fixture or self.fixture
        return lambda url: Response(200, {'Content-Type': 'text/html'}, f['listing_body'], url)

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=self.repeat())
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual({LIST_URL}, {d['meta_url'] for d in docs})  # crawl()'s meta_url, shared

    def test_short_paper_does_not_silently_become_complete(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        with patch('crawler.enumeration.pdftext.extract', fake_framework_pdftotext(f)):
            result, docs = run_split(SITE, fixture=f, end=self.repeat(f))
        self.assertEqual(0, result['stats']['saved_new'], result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
