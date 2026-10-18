"""santepubliquefrance-fr-presse: split (_enumeration_items / _enumeration_paper) must produce
exactly the papers crawl() saves; crawl() is unchanged and the external_id rule (Drupal node id,
else canonical path without index.php/) is kept."""
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'santepubliquefrance-fr-presse'
# Shape of the real page past the last one (/presse?page=57, 2026-10-18): the views wrapper, no rows.
EMPTY_PAGE = ('<html><body><main><div class="view-page-list view-list-press">'
              '<div class="view-empty"></div></div></main></body></html>')


def _tuple_transport_load(original):
    """crawl() expects _curl_get -> (body, effective_url) and reads self.detail_delay."""
    def load_wrapped(site):
        crawler, module = original(site)
        base = type(crawler)

        class Wrapped(base):
            @property
            def _curl_get(self):
                return self.__dict__['_transport']

            @_curl_get.setter
            def _curl_get(self, fn):
                self.__dict__['_transport'] = lambda url, *a, **k: (fn(url), url)

        crawler.__class__ = Wrapped
        crawler.detail_delay = 0
        return crawler, module
    return load_wrapped


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        import tests.collector_split_helpers as helpers
        with patch.object(helpers, 'load', _tuple_transport_load(helpers.load)):
            return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=57'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><main>maintenance</main></body></html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_rule_unchanged(self):
        expected, _ = self.native()
        # node id when drupalSettings has node/N, else the canonical path (index.php/ stripped)
        self.assertEqual(['39736', 'presse/offline-2-full'], [p['external_id'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
