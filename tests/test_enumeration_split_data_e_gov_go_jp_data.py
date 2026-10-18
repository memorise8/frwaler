"""data-e-gov-go-jp-data: CKAN package_search split (_enumeration_items / _enumeration_paper)
must produce exactly the papers crawl() saves; crawl() is unchanged. The plan reads the
same PDF-dataset query oldest first (metadata_created asc) instead of metadata_modified desc;
identity (external_id = CKAN id, post_number = name) is untouched."""
import json
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'data-e-gov-go-jp-data'
EMPTY = json.dumps({'success': True, 'result': {'count': 2, 'results': []}})


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?start=9500'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        for body in ('<html>maintenance</html>', json.dumps({'success': False, 'error': {'message': 'x'}})):
            try:
                error_page = parser.parse(Response(200, {'Content-Type': 'application/json'}, body, url))
            except Exception as exc:  # PermanentFailure listing_parse_error
                self.assertIn('listing_parse_error', str(getattr(exc, 'reason', exc)))
            else:
                self.assertTrue(error_page.extra.get('parse_error'))
                self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_is_ckan_id(self):
        expected, _ = self.native()
        self.assertEqual(['85f736f8-5597-4d8e-abe4-f6a4d8bb56af', '13a34bd4-385d-42c3-83fd-98ea880e5f36'],
                         [p['external_id'] for p in expected])
        self.assertEqual('clb_20140901_0031', expected[0]['post_number'])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'application/json'}, EMPTY, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
