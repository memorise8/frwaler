"""L3b-20261018: enumeration split for va-gov-vetdata.
One static report page (/vetdata/report.asp), every file link is a record built from
the page alone (paper_method); ?page=N past the start page is declared empty
(listing_marker div.accordion-container). All records share url = the report page,
identity is the file path (external_id), as in crawl().
Memory-only: no network or subprocess (the end-to-end run uses a temporary SQLite)."""
import json
import unittest

from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'va-gov-vetdata'
EMPTY = '<html><body><div id="innerContent"><div class="accordion-container"></div></div></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_short_abstract_skip_is_preserved(self):
        # The record abstract always carries the group/section context and suffix, so a
        # short description does not drop a record in crawl(); both paths must agree.
        from crawler.enumeration.adapters import clean_paper
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=2'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', p.listing['url']))
        self.assertTrue(error.extra.get('parse_error'))

    def test_identity_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['vetdata/docs/Synthetic/Report_2019.xlsx', 'vetdata/docs/Synthetic/Report_2018.pdf'],
                         [p['external_id'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, EMPTY, url)
        result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
