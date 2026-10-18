"""knmi-nl-kennis-en-datacentru: search-results page + detail page -> paper,
compared with the unchanged crawl(); plus the list-meta year fallback."""
import json
import unittest

from tests.l2b_g6_split_helpers import EmptyBodyEndRunChecks, PlainNativeSplitChecks

SITE = 'knmi-nl-kennis-en-datacentru'


class RegisteredSplit(PlainNativeSplitChecks, unittest.TestCase):
    SITE = SITE

    def test_year_falls_back_to_list_meta_when_detail_lacks_it(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False)
                       .replace(', Year: 2026', '').replace(', Year: 2025', ''))
        papers = self.pipeline(f)
        self.assertEqual(['2026-01-01', '2025-01-01'], [p['published_date'] for p in papers])
        self.assertEqual(['pub-een', 'pub-twee'], [p['external_id'] for p in papers])

    def test_row_without_intro_is_skipped_like_crawl(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace('Korte intro een...', ''))
        expected, _ = self.native(f)
        self.assertEqual(['pub-twee'], [p['external_id'] for p in expected])
        self.assertEqual(['pub-twee'], [p['external_id'] for p in self.pipeline(f)])


class RegisteredSplitRun(EmptyBodyEndRunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
