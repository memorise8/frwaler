"""L3b-20261018: enumeration split for biblio-naturalsciences-be-rbins-publications.
crawl() POSTs b_start:int=N to @@faceted_query; the same parameter as a GET query
returns the same Plone result batches (250 rows; the last batch absorbs orphans).
Rows are parsed with _parse_list_html; the detail conversion is copied verbatim
(external_id = URL slug). An answer past the end keeps .biblio-datatable."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'biblio-naturalsciences-be-rbins-publications'
EMPTY = '<html><body><div class="biblio-datatable"></div></body></html>'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, posts = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def fetch(url, method='GET', post_data='', retries=3):
            if method == 'POST':
                posts.append(post_data)
                return fixture['listing_body'] if len(posts) == 1 else EMPTY
            return fixture['details'].get(url)
        crawler._curl_fetch = fetch
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, posts

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['articlereference.2031-02-13.1234567890', 'synthetic_article_2030'],
                         [p['external_id'] for p in self.raw_papers])
        self.assertEqual(['1234567890', None], [p['post_number'] for p in self.raw_papers])

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is a bibliographic description built from title/authors/year (never short here).
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        self.assertEqual(2, len(self.pipeline()))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?b_start%3Aint=5000'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
        self.assertTrue(foreign.extra.get('parse_error'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
