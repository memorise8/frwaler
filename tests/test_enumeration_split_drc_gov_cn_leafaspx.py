"""L4-20261018: enumeration split for drc-gov-cn-leafaspx (GetPageDocuments.ashx JSON, 100 rows/page).
The live API clamps: a page past the last one repeats the last page (2026-10-18: page 40 = page 39),
which the last_page_backward strategy treats as the end. Detail pages are GB2312 bytes."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'drc-gov-cn-leafaspx'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() reads self.detail_delay, normally set by __init__ (not run here).
        from unittest.mock import patch
        from tests import collector_split_helpers as helpers
        real_load = helpers.load

        def load_with_delay(site):
            crawler, module = real_load(site)
            crawler.detail_delay = 0
            return crawler, module
        with patch.object(helpers, 'load', load_with_delay):
            return super().native(fixture)

    def test_gb2312_detail_bytes_decode_like_crawl(self):
        crawler, _ = load(self.SITE)
        url, body = next(iter(self.fixture['details'].items()))
        item = {'row': {'DocId': 7001, 'DocViewUrl': url, 'Summary': ''}, 'url': url, 'doc_id': '7001'}
        from_bytes = crawler._enumeration_paper(item, body.encode('gb18030'))
        from_text = crawler._enumeration_paper(item, body)
        self.assertEqual(from_text, from_bytes)
        self.assertEqual('7001', from_bytes['external_id'])

    def test_post_number_is_doc_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['7001', '7000'], [p['external_id'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'application/json', '[{"rows": [], "totalpage": 1}]')})


if __name__ == '__main__':
    unittest.main()
