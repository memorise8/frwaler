"""L3b-20261018: enumeration split for lavoro-gov-it-stampa-e-media.
Comunicati list ?page=N (0-based, 20 rows), detail pages give the Drupal node id
(external_id, slug fallback) exactly as crawl(); a page past the end has no
.views-row and is proved by the listing_marker (main)."""
import json
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'lavoro-gov-it-stampa-e-media'
EMPTY = '<html><body><main></main></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['90002', '90001'], [p['external_id'] for p in self.raw_papers])
        self.assertEqual(['2031-02-13', '2031-01-09'], [p['published_date'] for p in self.raw_papers])

    def test_slug_fallback_without_node_id_like_original(self):
        f = json.loads(json.dumps(self.fixture).replace('data-history-node-id', 'data-other'))
        expected, _ = self.native(f)
        self.assertEqual(['synthetic-90002', 'synthetic-90001'], [p['external_id'] for p in expected])
        self.assertEqual(['synthetic-90002', 'synthetic-90001'], [p['external_id'] for p in self.pipeline(f)])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
