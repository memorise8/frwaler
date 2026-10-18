"""NR2-20261018: publications-idiap-ch-publications enumeration split (crawl() unchanged), memory-only.
The articles list is one page (no pager); the site answers any /articles/... path with the same
list, so the X1 path-paged plan ends at page 1 by wrap."""
import unittest

from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = 'publications-idiap-ch-publications'

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # The single articles page always lists publications; a page without any
        # div.publication_summary is never read as an empty end page.
        from crawler.enumeration.http import PermanentFailure, Response
        p, parser = self._parser()
        for body in (self.fixture['empty_body'], '<html>maintenance</html>'):
            try:
                page = parser.parse(Response(200, {'Content-Type': 'text/html'}, body, p.listing['first_page_url']))
            except PermanentFailure as exc:
                self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
            else:
                self.assertFalse(page.extra.get('explicit_empty', False))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'publications-idiap-ch-publications'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
