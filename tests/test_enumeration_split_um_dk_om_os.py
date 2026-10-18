"""L3b-20261018: enumeration split for um-dk-om-os.
One static listing page of annual-report PDFs (no pagination, no detail pages); the
paper is built from the listing row (paper_method). ?page=N past the start page is
declared empty (listing_marker main). All papers share url = the listing page, as in
crawl(); identity is the media hash (external_id).
Memory-only: no network or subprocess (the end-to-end run uses a temporary SQLite)."""
import json
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split
from crawler.enumeration.http import Response

SITE = 'um-dk-om-os'
EMPTY = '<html><body><main><p>Ingen</p></main></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_short_abstract_skip_is_preserved(self):
        # crawl() always prefixes the year line to the page description (or its fallback),
        # so a short page text never drops a record; both paths must agree.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        from crawler.enumeration.adapters import clean_paper
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_identity_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['syn00002', 'syn00001'], [p['external_id'] for p in expected])
        self.assertEqual(['2025', '2002'], [p['post_number'] for p in expected])

    def test_page_two_is_empty(self):
        crawler, _ = load(SITE)
        url = self.plan['sites'][SITE]['listing']['url']
        self.assertEqual([], crawler._enumeration_items(self.fixture['listing_body'], url + '?page=2'))
        self.assertEqual(2, len(crawler._enumeration_items(self.fixture['listing_body'], url + '?page=1')))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, EMPTY, url)
        result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
