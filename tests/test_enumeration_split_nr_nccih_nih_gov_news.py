"""NR-20261018: nccih-nih-gov-news enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nccih-nih-gov-news'

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # Gatsby page-data: the page after the last one is a 404 (end); a 200 JSON with no
        # edges is never read as an end, and an HTML error page is a listing parse error.
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.urls import build_url
        p, parser = self._parser()
        url = p.listing['first_page_url']
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertFalse(page.extra.get('explicit_empty', False))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nccih-nih-gov-news'
    END_STATUS = 404


if __name__ == '__main__':
    unittest.main()
