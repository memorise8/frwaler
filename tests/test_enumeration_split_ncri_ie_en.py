"""ncri-ie-en: split (_enumeration_items / _enumeration_paper) must produce the papers crawl()
saves; crawl() is unchanged. Only difference: crawl() reads the PDF filename with a HEAD
(curl subprocess) that the enumeration runner forbids, so original_filename stays None."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'ncri-ie-en'


def run_native(case, fixture, head=None):
    crawler, _ = load(SITE)
    captured, calls = [], []

    def transport(url, *args, **kwargs):
        calls.append(url)
        if url in fixture['details']:
            return fixture['details'][url]
        if url.endswith('&page=0'):
            return fixture['listing_body']
        return fixture['empty_body']
    crawler._delay = 0
    crawler._last_save_created = True
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    crawler._curl_get = transport
    crawler._curl_head = head or (lambda url: {})
    with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
        result = crawler.crawl(limit=10)
    case.assertEqual(len(captured), result)
    return captured, calls


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        return run_native(self, fixture or self.fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?keywords=&section=3&sort_bef_combine=relevance_DESC&page=13'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><p>maintenance</p></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))

    def test_identity_is_drupal_node_id(self):
        expected, _ = self.native()
        self.assertEqual(['251', '262'], [p['external_id'] for p in expected])

    def test_only_head_filename_differs_from_crawl_with_head(self):
        head = lambda url: {'content-disposition': 'attachment; filename=report.pdf'}
        with_head, _ = run_native(self, self.fixture, head=head)
        actual = self.pipeline()
        self.assertEqual(['report.pdf', 'report.pdf'], [p['original_filename'] for p in with_head])
        for crawl_paper, split_paper in zip(with_head, actual):
            crawl_paper = clean_paper(crawl_paper)
            meta_c, meta_s = json.loads(crawl_paper.pop('metadata')), json.loads(split_paper.pop('metadata'))
            self.assertEqual(None, split_paper.pop('original_filename'))
            crawl_paper.pop('original_filename')
            self.assertEqual(crawl_paper, split_paper)
            self.assertIsNone(meta_s.pop('originalFilename'))
            meta_c.pop('originalFilename')
            self.assertEqual(meta_c, meta_s)

    def test_file_row_is_skipped_without_parsing(self):
        crawler, _ = load(SITE)
        self.assertIsNone(crawler._enumeration_paper('%PDF-1.4 binary', {'url': 'https://www.ncri.ie/en/media/937/download'}))
        self.assertNotIn('_curl_head', crawler.__dict__)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        body = self.fixture['empty_body']
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(['251', '262'], sorted(d['post_number'] for d in docs))


if __name__ == '__main__':
    unittest.main()
