"""L3b-20261018: enumeration split for asiapacific-ca-media.
One year-grouped list page (no server-side pagination, crawl() reads one page):
page > 1 is declared empty and proved by the list page's <main>."""
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'asiapacific-ca-media'
EMPTY = '<html><body><main class="main"></main></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_page_two_is_declared_empty(self):
        crawler, _ = load(self.SITE)
        url = 'https://www.asiapacific.ca/media/press-releases?page=2'
        self.assertEqual([], crawler._enumeration_items(self.fixture['listing_body'], url))
        self.assertEqual(2, len(crawler._enumeration_items(self.fixture['listing_body'], url[:-7])))

    def test_post_number_is_node_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['90002', '90001'], [p['external_id'] for p in expected])
        self.assertEqual(['90002', '90001'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
