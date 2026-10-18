import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.http import PermanentFailure, Response
from tests.collector_split_helpers import load, run_split, split_fixture
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'eng-mod-gov-cn-2025xb'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() with a memory transport (its _detail_delay is a method, so the shared
        helper's attribute overrides cannot be used here)."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(1, len(listing_calls))  # page count 1: crawl() reads index.html only
        return captured, calls

    def test_multi_page_channel_is_a_listing_error_not_a_partial_list(self):
        """index_N.html pages are unreachable for a query-parameter plan: a page count
        other than 1 must stop the run instead of reading page 1 only."""
        crawler, _ = load(self.SITE)
        body = self.fixture['listing_body'].replace("createPageHTML(\n'1',", "createPageHTML(\n'3',")
        self.assertNotEqual(body, self.fixture['listing_body'])
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(body)
        self.assertEqual('listing_parse_error:channel_not_single_page', ctx.exception.reason)

    def test_pdf_without_file_name_fails_the_item_instead_of_guessing(self):
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'])
        url, html = next(iter(self.fixture['details'].items()))
        long_name = 'x' * 230 + '.pdf'
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_paper(html.replace('law1.pdf', long_name), rows[0])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def test_ignored_page_parameter_ends_as_wrap(self):
        """The live channel ignores ?page=N and answers the same single page: the
        strategy ends on the repeat (wrap) after the one real page."""
        fixture = split_fixture(SITE)
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], url)
        result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_wrap'), result)
        self.assertEqual(2, len(docs))
