import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'datacatalogue-adruk-org-browser'

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # A page past the end is the API's page object with an empty "content" list.
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        from tests.collector_split_helpers import load
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"content": [], "totalPages": 1}', p.listing['url']))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        for body in ('[]', '{"code": "error"}'):
            with self.subTest(body=body), self.assertRaises(PermanentFailure):
                parser.parse(Response(200, {'Content-Type': 'application/json'}, body, p.listing['url']))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'datacatalogue-adruk-org-browser'
    END_PAGES = {**SplitRunChecks.END_PAGES,
                 'datacatalogue-adruk-org-browser': (200, 'application/json', '{"content": [], "totalCount": 2, "totalPages": 1}')}
