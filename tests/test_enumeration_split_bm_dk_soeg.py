"""bm-dk-soeg: split (_enumeration_items / _enumeration_detail / _enumeration_paper) must produce
exactly the papers crawl() saves from the Ankiro search API (GET, startIndex offset paging);
crawl() is unchanged and post_number stays the Ankiro document Id."""
import json
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'bm-dk-soeg'
# Shape of the real answer past the last item (startIndex=91 of TotalResults 88, 2026-10-18).
EMPTY_PAGE = json.dumps({'TotalResults': 88, 'Offset': 90, 'Documents': []})


def _end(url):
    return Response(200, {'Content-Type': 'application/json'}, EMPTY_PAGE, url)


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?startIndex=91'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        html_error = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertEqual('invalid_json', html_error.extra.get('parse_error'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '{"Message": "error"}', url))

    def test_identity_is_ankiro_id(self):
        expected, _ = self.native()
        self.assertEqual(['248045', '246584'], [p['post_number'] for p in expected])
        self.assertEqual('https://bm.dk/media/a1/offline-one.pdf', expected[0]['pdf_url'])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=_end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
