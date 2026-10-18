"""archives-nationales-culture-gouv-fr-presse: list-only split (_enumeration_items /
_enumeration_paper) must produce exactly the papers crawl() saves; crawl() is unchanged."""
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'archives-nationales-culture-gouv-fr-presse'
# Real page past the last one (page=4, 2026-10-18): the view container is present, no cards.
EMPTY_PAGE = ('<html><body><main><div class="views-element-container">'
              '<div class="fr-grid-row an-views-view-liste-publications"><h1>Presse</h1></div>'
              '<p class="fr-text--xs">27 résultat(s)</p></div></main></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() fetches through _fetch_page (cookie handshake around _curl_get, which
        # returns (headers, body)); the fixture serves the listing body directly.
        import tests.collector_split_helpers as helpers
        original = helpers.load

        def load_plain(site):
            crawler, module = original(site)
            crawler._fetch_page = lambda url: crawler._curl_get(url)
            return crawler, module
        with patch.object(helpers, 'load', load_plain):
            return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=4'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_PAGE, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        # Maintenance or bot-mitigation pages lack the view container: parse error, not an end.
        for body in ('<html>maintenance</html>', "<html><body><a href='/redirect_x'>go</a></body></html>"):
            error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url))
            self.assertTrue(error_page.extra.get('parse_error'))
            self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_rule_matches_crawl(self):
        expected, _ = self.native()
        # VersionId when present, else sha256(pdf_url)[:20]; post_number None in that case.
        self.assertEqual('1790003638210009', expected[0]['external_id'])
        self.assertIsNone(expected[1]['post_number'])
        self.assertEqual(20, len(expected[1]['external_id']))


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
