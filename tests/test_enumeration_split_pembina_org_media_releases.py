import copy
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'pembina-org-media-releases'

    def test_short_abstract_skip_is_preserved(self):
        # crawl() prepends the subtitle (h2) before the 50-character check, so a short
        # body alone does not skip; drop the subtitle too, as crawl() would then skip.
        f = copy.deepcopy(self.fixture)
        for url, body in f['details'].items():
            f['details'][url] = body.replace(self.fixture['long_text'], 'Short.').replace('<h2>', '<h5>').replace('</h2>', '</h5>')
        f['listing_body'] = f['listing_body'].replace('Teaser for item-1', '').replace('Teaser for item-2', '')
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_rendered_cards_that_do_not_parse_are_not_the_end(self):
        crawler, _ = load(self.SITE)
        changed = self.fixture['listing_body'].replace('/media-release/', '/press/')
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(changed)
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_live_no_results_page_is_empty(self):
        crawler, _ = load(self.SITE)
        body = ('<html><body><main><div class="results"><p>Your search yielded no results. Check spelling, '
                'try using different search terms, or expand the search criteria.</p></div></main></body></html>')
        self.assertEqual([], crawler._enumeration_items(body))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'pembina-org-media-releases'
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{
        'pembina-org-media-releases': (200, 'text/html', '<html><body><main><div class="results">'
                                       '<p>Your search yielded no results.</p></div></main></body></html>')})
