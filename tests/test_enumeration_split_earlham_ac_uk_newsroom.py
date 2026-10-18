"""earlham-ac-uk-newsroom: the plan reads /newsroom?page=N (same cards as crawl()'s
Drupal AJAX chunk); _enumeration_items/_enumeration_paper must give exactly the papers
crawl() saves. Card and detail parsing were moved verbatim into _parse_cards and
_parse_detail_raw (crawl() unchanged)."""
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'earlham-ac-uk-newsroom'
# A page past the last one: the featured story in the view header only, no own stories.
FEATURED_ONLY = ('<html><body><main><div class="views-element-container"><div class="view">'
                 '<div class="view-header"><a class="earlham-card-link" href="/news/featured">'
                 '<article aria-labelledby="card-label-1"></article></a></div>'
                 '<div class="view-content"></div></div></div></main></body></html>')


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() reads the Drupal AJAX command list; serve the same cards in that wrapper.
        import tests.collector_split_helpers as helpers
        fixture = json.loads(json.dumps(fixture or self.fixture))
        fixture['listing_body'] = json.dumps([{'command': 'insert',
                                               'selector': '.js-view-dom-id-8b6c3df7388cc406a4463a9345695be7d5d653fd5a0df3a32e3e1305e811c391',
                                               'data': fixture['listing_body']}])
        return super().native(fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?page=48'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, FEATURED_ONLY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        first = parser.parse(Response(200, {'Content-Type': 'text/html'}, FEATURED_ONLY, p.listing['url'] + '?page=0'))
        self.assertEqual(1, len(first.items))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))

    def test_identity_is_node_id(self):
        expected, _ = self.native()
        self.assertEqual(['24311', '24319'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, FEATURED_ONLY, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
