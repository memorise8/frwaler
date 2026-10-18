import unittest

from crawler.enumeration.http import PermanentFailure, Response
from tests.collector_split_helpers import load, run_split, split_fixture
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'mof-gov-cn-en'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_multi_page_channel_is_a_listing_error_not_a_partial_list(self):
        """index_N.htm pages are unreachable for a query-parameter plan: a page count
        other than 1 must stop the run instead of reading page 1 only."""
        crawler, _ = load(self.SITE)
        body = self.fixture['listing_body'].replace('var countPage = 1', 'var countPage = 2')
        self.assertNotEqual(body, self.fixture['listing_body'])
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(body)
        self.assertEqual('listing_parse_error:channel_not_single_page', ctx.exception.reason)


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
