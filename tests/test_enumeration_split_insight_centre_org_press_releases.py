import unittest

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks

SITE = 'insight-centre-org-press-releases'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # One listing page that is never empty: a page without the press block is an error.
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = helpers.load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', p.listing['url']))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    # The page ignores ?page=N and repeats itself (wrap): page 1 is the last page.
    END_PAGES = {**SplitRunChecks.END_PAGES,
                 SITE: (200, 'text/html', helpers.split_fixture(SITE)['listing_body'])}
