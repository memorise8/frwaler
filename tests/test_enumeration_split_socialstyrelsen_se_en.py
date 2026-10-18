"""socialstyrelsen-se-en: single /en/publications/ page (React props), detail page + PDF per
item (ext:two_step_detail with ext:two_step_keep_first). The abstract stays crawl()'s own
pdfplumber text of the first 5 PDF pages (no pdftotext). crawl() (unchanged) and the shipped
plan are compared on synthetic data; no network."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load, run_split
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.test_enumeration_extensions_x2 import make_pdf

SITE = 'socialstyrelsen-se-en'
B = 'https://www.socialstyrelsen.se'
DETAIL_ONE = B + '/publikationer/synthetic-report-one-2031-1-90001/'
PDF_ONE = B + '/contentassets/0000000000000000000000000000aaaa/2031-1-90001.pdf'
PDF_TWO = B + '/contentassets/0000000000000000000000000000bbbb/2030-12-90002.pdf'


def with_pdf_text(fixture, text):
    f = copy.deepcopy(fixture)
    for url in (PDF_ONE, PDF_TWO):
        f['details'][url] = make_pdf(text).decode('latin-1')
    return f


def as_bytes(value):
    return value.encode('latin-1') if value.startswith('%PDF') else value.encode('utf-8')


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with memory transports for its _curl_text/_curl_bytes helpers."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, calls = [], []

        def text(url, retries=3):
            calls.append(url)
            return fixture['details'][url] if url in fixture['details'] else fixture['listing_body']

        def raw(url, retries=3):
            calls.append(url)
            value = fixture['details'].get(url)
            return None if value is None else as_bytes(value)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_text = text
        crawler._curl_bytes = raw
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def pipeline(self, fixture=None, redirect=False):
        """SplitChecks.pipeline with PDF bodies served as bytes."""
        fixture = copy.deepcopy(fixture or self.fixture)
        return super().pipeline(fixture, redirect)

    def test_plan_selector_is_the_checked_one(self):
        crawler, _ = load(SITE)
        self.assertEqual(crawler._ENUM_PDF_SELECTOR,
                         self.plan['sites'][SITE]['adapter']['second_step']['selector'])

    def test_both_pages_feed_the_paper(self):
        expected, calls = self.native()
        self.assertEqual(4, len([u for u in calls if u in self.fixture['details']]))
        actual = self.pipeline()
        self.assertEqual('2031-01-21', actual[0]['published_date'])  # 'Publicerad' on the detail page
        self.assertEqual('2030-12-10', actual[1]['published_date'])  # no span: listing date, like crawl()
        self.assertTrue(actual[0]['abstract'].startswith(self.fixture['long_text']))  # PDF text
        self.assertEqual(PDF_TWO, actual[1]['pdf_url'])  # fallback /contentassets/ link
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(expected, self.raw_papers)

    def test_short_abstract_skip_is_preserved(self):
        f = with_pdf_text(self.fixture, 'Short.')
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_identity_is_article_number(self):
        expected, _ = self.native()
        self.assertEqual(['2031-1-90001', '2030-12-90002'], [paper_identity(p)[0] for p in expected])
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in self.pipeline()])

    def test_other_pdf_than_crawl_fails_loudly(self):
        crawler, _ = load(SITE)
        page = self.fixture['details'][DETAIL_ONE]
        with self.assertRaises(ValueError):
            crawler._enumeration_paper(b'', page, DETAIL_ONE, DETAIL_ONE,
                                       {'url': DETAIL_ONE, 'record': {'articleNumber': 'x'}})
        swapped = page.replace('<a href="/contentassets/aaa/other.docx">Word</a>',
                               '<a href="/contentassets/aaa/annex.pdf">Annex</a>')
        swapped = swapped.replace(' class="publication-wrapper__main-doc link"', '')
        swapped += '<a class="publication-wrapper__main-doc" href="/contentassets/zz/main.pdf">x</a>'
        with self.assertRaises(ValueError):
            crawler._enumeration_paper(b'', swapped, PDF_ONE, DETAIL_ONE,
                                       {'url': DETAIL_ONE, 'record': {'articleNumber': 'x'}})

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """Single listing page: no empty end page (the end is the repeated page); a page
        without the publication list props is a listing error, never an empty end."""
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], p.listing['url']))
        self.assertEqual(2, len(page.items))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', p.listing['url']))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def repeat(self, fixture=None):
        f = fixture or self.fixture
        return lambda url: Response(200, {'Content-Type': 'text/html'}, f['listing_body'], url)

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=self.repeat())
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
        self.assertEqual({PDF_ONE, PDF_TWO}, {d['pdf_url'] for d in docs})

    def test_short_paper_does_not_silently_become_complete(self):
        f = with_pdf_text(self.fixture, 'Short.')
        result, docs = run_split(SITE, fixture=f, end=self.repeat(f))
        # crawl() skips short abstracts; the run records each skip as a failed item, never a save.
        self.assertEqual(2, result['stats']['failed_permanent'], result)
        self.assertEqual(0, result['stats']['saved_new'], result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
