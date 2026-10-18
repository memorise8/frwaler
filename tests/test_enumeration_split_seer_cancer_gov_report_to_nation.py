"""L3b-20261018: enumeration split for seer-cancer-gov-report_to_nation — the single archive page
(crawl() reads one page; ?page=N returns the same page, so the runner sees a wrap at page 2).
Entries become papers with crawl()'s paper code (post_number = year slug such as 1975-2017)."""
import json
import subprocess
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture

SITE = 'seer-cancer-gov-report_to_nation'
FIXTURE = split_fixture(SITE)
LIST_URL = 'https://seer.cancer.gov/report_to_nation/previous.html'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() fetches through the module's `_curl` (subprocess.run); serve the fixture page.
        fixture = fixture or self.fixture
        def fake_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout=fixture['listing_body'].encode('utf-8'), stderr=b'')
        with patch('subprocess.run', fake_run):
            return super().native(fixture)

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['2027-2031', '2026-2030'], [p['post_number'] for p in self.raw_papers])

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is the whole citation block (links included); an entry whose block
        # is shorter than 50 characters is skipped by crawl() and by the adapter alike.
        import re
        body = re.sub(r'(<h2>2026-2030[^<]*</h2>).*?(</main>)', r'\1<p>Short.</p>\2', self.fixture['listing_body'], flags=re.S)
        f = dict(self.fixture, listing_body=body)
        expected, _ = self.native(f)
        self.assertEqual(['2027-2031'], [p['post_number'] for p in expected])
        crawler, _ = load(self.SITE)
        self.assertEqual(['2027-2031'], [r['slug'] for r in crawler._enumeration_items(body, LIST_URL)])
        self.assertIsNone(crawler._enumeration_paper(dict(crawler._enumeration_items(body, LIST_URL)[0], abstract='Short.')))

    def test_repeated_slug_keeps_first_entry_like_crawl(self):
        f = dict(self.fixture, listing_body=self.fixture['duplicate_body'])
        expected, _ = self.native(f)
        self.assertEqual(['2027-2031', '2026-2030'], [p['post_number'] for p in expected])
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><main id="content"></main></html>', LIST_URL))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', LIST_URL))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    # ?page=2 answers with the same single page: the runner reads it as a wrap.
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', FIXTURE['listing_body'])})


if __name__ == '__main__':
    unittest.main()
