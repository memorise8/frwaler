"""L3b-20261018: enumeration split for bankofcanada-ca-publications.
Listing = the browse page itself, GET /publications/browse/?content_type[]=21396&mt_page=N
(10 rows); crawl() reads the same article.media rows from the widget's AJAX JSON."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'bankofcanada-ca-publications'
EMPTY = ('<html><body><main><div class="mtw archive paging-paged" id="multitaxonomytaglist-cfct-module-synthetic">'
         '<span class="num-results">2</span></div></main></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl(): start page -> browse page -> widget AJAX JSON (page 1) -> details."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls = [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            if 'frontend-ajax.php' in url:
                self.assertIn('mt_page=1', url)
                return json.dumps({'content': fixture['listing_body']})
            return fixture['listing_body']  # start page and browse page
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def test_short_abstract_skip_is_preserved(self):
        # The abstract joins meta description, list excerpt and body summary: a short
        # body alone does not always skip; crawl() and the adapter must agree.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        bare = json.loads(json.dumps(f).replace('Synthetic description', 'D').replace('Synthetic excerpt', 'E'))
        expected, _ = self.native(bare)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(bare))

    def test_post_number_is_list_post_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['990002', '990001'], [p['external_id'] for p in expected])

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = 'https://www.bankofcanada.ca/publications/browse/?content_type%5B%5D=21396&mt_page=4'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertEqual('listing_structure_missing', error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_added_listing_rejects_wrong_shape(self):
        c, _ = load(self.SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items({'error': 'not a listing'}, 'https://www.bankofcanada.ca/publications/browse/')


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
