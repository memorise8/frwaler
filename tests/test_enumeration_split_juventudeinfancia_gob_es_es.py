"""L4-20261018: enumeration split for juventudeinfancia-gob-es-es (Drupal ?page=N, 0-based).
Past the last page the site answers 200 with the listing view container but no rows."""
import unittest
from urllib.parse import urlencode

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'juventudeinfancia-gob-es-es'
EMPTY_VIEW = ('<html><body><main><div class="view view-carrousel view-id-carrousel '
              'view-display-id-page_1"></div></main></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?' + urlencode({'page': 65})
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY_VIEW, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))
        # View rows present but unparseable: a structure change, never the end.
        changed = EMPTY_VIEW.replace('"></div>', '"><div class="view-content"><p>row</p></div></div>')
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(changed)

    def test_post_number_is_slug_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['offline-1', 'offline-2'], [p['external_id'] for p in expected])
        self.assertEqual(['101', '102'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY_VIEW)})


if __name__ == '__main__':
    unittest.main()
