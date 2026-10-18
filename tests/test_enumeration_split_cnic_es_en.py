"""L3b-20261018: enumeration split for cnic-es-en (Drupal GET /en/actualidad/noticias?page=N,
0-based, 10 rows). crawl() fetches with self._curl; the memory-only run routes it to the
fake transport. A short detail body never skips: crawl() builds the abstract from intro + source."""
import unittest
from unittest.mock import patch

from tests import collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'cnic-es-en'
EMPTY = '<html><body><div class="view view-Buscadores-noticias view-id-Buscadores"></div></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        real_load = helpers.load

        def load_with_curl(site):
            crawler, module = real_load(site)
            crawler._curl = lambda url, *a, **k: crawler._curl_get(url)
            return crawler, module
        with patch.object(helpers, 'load', load_with_curl):
            return super().native(fixture)

    def test_short_abstract_skip_is_preserved(self):
        # crawl() never skips here: a short body is replaced by intro + source text (>= 50 chars).
        import json
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertTrue(expected[0]['abstract'].startswith('Intro text 9001.'))
        self.assertEqual([helpers.clean_paper(p) for p in expected], self.pipeline(f))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=99'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_post_number_is_node_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['9001', '9000'], [p['external_id'] for p in expected])
        self.assertEqual(['9001', '9000'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
