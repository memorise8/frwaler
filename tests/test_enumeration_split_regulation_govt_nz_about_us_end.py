"""regulation-govt-nz-about-us: the live page past the last offset is an explicit empty page.

Excerpt of the real start=360 page (2026-10-18, 337 publications): the search chrome and
an empty ``.search__results > ol`` are rendered inside <main>. No network.
"""
import unittest

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

import json
from pathlib import Path

SITE = 'regulation-govt-nz-about-us'
ROOT = Path(__file__).resolve().parents[1]
URL = ('https://www.regulation.govt.nz/about-us/our-publications/search/?query=&order=relevance'
       '&publication_type_id%5B110%5D=110&sort_date_from=&sort_date_to=&start=360')
PAST_END = ('<html><body><main><h1>Our publications</h1>'
            '<div class="search__count"><span class="search__count--bold">361 - 337</span> of '
            '<span class="search__count--bold">337</span> publication(s)</div>'
            '<div class="search__results" aria-live="polite"><ol>\n</ol>'
            '<div class="search__pagination"><nav class="pagination"><ul>'
            '<li><a class="pagination__link" href="?start=0">1</a></li></ul></nav></div></div>'
            '</main></body></html>')
CHANGED_ROWS = PAST_END.replace('<ol>\n</ol>', '<ol><li class="result-card"><a href="/x/">X</a></li></ol>')


class RegulationPastEnd(unittest.TestCase):
    def parser(self):
        plan = json.loads((ROOT / 'docs/examples/collector-splits' / f'{SITE}.json').read_text())
        p = site_plan(plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))

    def test_live_past_end_page_is_explicit_empty(self):
        page = self.parser().parse(Response(200, {'Content-Type': 'text/html'}, PAST_END, URL))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))

    def test_unrecognised_rows_are_not_mistaken_for_the_end(self):
        with self.assertRaises(PermanentFailure) as ctx:
            self.parser().parse(Response(200, {'Content-Type': 'text/html'}, CHANGED_ROWS, URL))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
