"""NR2-20261018: english-www-gov-cn-archive enumeration split (crawl() unchanged), memory-only.
List pages are /archive/statecouncilgazette/ then page_N.html (X1 path paging)."""
import json
import unittest

from tests.collector_split_helpers import load
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks

SITE = 'english-www-gov-cn-archive'


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = SITE
    TRANSPORTS = NRSplitChecks.TRANSPORTS + ('_curl_text',)

    def test_short_abstract_skip_is_preserved(self):
        # crawl() pads a 1..99-character abstract with an archive sentence instead of skipping;
        # the split must do exactly the same (and skip only below 50 characters after padding).
        from crawler.enumeration.adapters import clean_paper
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertTrue(all('Archive record for' in p['abstract'] for p in expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_list_page_number_comes_from_the_path(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'],
                                          'https://english.www.gov.cn/archive/statecouncilgazette/page_7.html')
        self.assertEqual([7, 7], [r['list_page'] for r in rows])
        rows = crawler._enumeration_items(self.fixture['listing_body'],
                                          'https://english.www.gov.cn/archive/statecouncilgazette/')
        self.assertEqual([1, 1], [r['list_page'] for r in rows])


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
