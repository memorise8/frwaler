"""who-int-publications: OData rows (/api/hubs/publications, $skip offset by 20) + item
page through the unchanged crawl() vs the plan adapter. Memory only."""
import unittest

from crawler.enumeration.guard import guarded
from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'who-int-publications'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_identity_is_item_path(self):
        papers = self.pipeline()
        self.assertEqual(self.fixture['expected_ids'], [p['external_id'] for p in papers])
        self.assertEqual(list(self.fixture['details']), [p['url'] for p in papers])

    def test_rows_without_item_path_are_skipped(self):
        import json
        crawler, _ = load(self.SITE)
        with guarded():
            rows = crawler._enumeration_items(json.loads(self.fixture['listing_body']))
        self.assertEqual(self.fixture['expected_ids'], [r['external_id'] for r in rows])

    def test_offset_paging_is_declared(self):
        listing = self.plan['sites'][self.SITE]['listing']
        self.assertEqual(('$skip', 'offset', 20), (listing['page_param'], listing['page_mode'], listing['page_size']))
        self.assertIn('$top=20', listing['url'])

    def test_detail_fetcher_is_restored(self):
        import json
        crawler, _ = load(self.SITE)
        item = crawler._enumeration_items(json.loads(self.fixture['listing_body']))[0]
        with guarded():
            crawler._enumeration_paper(self.fixture['details'][item['url']], item)
        self.assertNotIn('_curl_get', crawler.__dict__)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE
