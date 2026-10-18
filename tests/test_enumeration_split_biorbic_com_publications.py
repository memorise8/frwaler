"""L3b-20261018: enumeration split for biorbic-com-publications — the single /publications/ page
(Ninja Tables, every row server-side rendered; crawl() reads one page). ?page=N redirects to the
same page, so the runner sees a wrap at page 2 and page 1 is the last page. Rows become papers with
crawl()'s own _build_paper (post_number = data-row_id)."""
import json
import subprocess
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture

SITE = 'biorbic-com-publications'
FIXTURE = split_fixture(SITE)


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() fetches through `curl` (subprocess.run); serve the fixture listing instead.
        fixture = fixture or self.fixture
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=fixture['listing_body'].encode('utf-8'), stderr=b'')
        with patch('subprocess.run', fake_run):
            return super().native(fixture)

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['9001', '9000'], [p['post_number'] for p in self.raw_papers])

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is a bibliographic template around the row fields: a short title
        # never makes a row short, in crawl() or in the adapter.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_repeated_reference_url_keeps_first_row_like_crawl(self):
        f = dict(self.fixture, listing_body=self.fixture['duplicate_body'])
        expected, _ = self.native(f)
        self.assertEqual(['9001', '9000'], [p['post_number'] for p in expected])
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_empty_title_row_is_skipped_like_crawl(self):
        f = dict(self.fixture, listing_body=self.fixture['duplicate_body'].replace(
            self.fixture['long_text'] + ' 9002 repeat', ''))
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(f['listing_body'], 'https://biorbic.com/publications/')
        self.assertEqual(['9001', '9000'], [r['row_id'] for r in rows])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url']
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    # ?page=2 is redirected to the same single page: the runner reads it as a wrap.
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', FIXTURE['listing_body'])})


if __name__ == '__main__':
    unittest.main()
