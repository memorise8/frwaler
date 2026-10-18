"""caissedesdepots-fr-communiques-de-press: enumeration entry points reuse crawl()'s
_parse_page/_paper unchanged. Memory only (fixture = small excerpt of the live pages)."""
import ast
import copy
import hashlib
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

ROOT = Path(__file__).resolve().parents[1]
SITE = 'caissedesdepots-fr-communiques-de-press'
LIST_URL = 'https://www.caissedesdepots.fr/presse/communiques-de-presse'
PLAN = {'version': 1, 'sites': {SITE: {
    'strategy': 'last_page_backward',
    'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                'detail_method': '_enumeration_detail', 'detail_args': ['html', 'item', 'url']},
    'listing': {'url': LIST_URL, 'page_param': 'page', 'start_page': 0,
                'parser': {'type': 'adapter', 'key_field': 'url', 'url_field': 'url', 'title_field': 'title',
                           'date_field': 'date_iso', 'listing_marker': 'main .results__title'}},
    'last_page': {'overlap_pages': 1}}}}


class CdcSplit(unittest.TestCase):
    def setUp(self):
        self.fx = json.loads((ROOT / 'tests/fixtures/l2b_g2' / (SITE + '.json')).read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child',
                     'sqlite3.connect'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)
        self.crawler, _ = load(SITE)
        self.crawler.site_id = SITE
        self.crawler._delay = 0
        self.crawler.delivery_should_cancel = lambda: False

    def test_items_equal_crawl_parse_page(self):
        expected = self.crawler._parse_page(self.fx['listing_html'])
        self.assertEqual(2, len(expected))
        with guarded():
            self.assertEqual(expected, self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL + '?page=0'))

    def test_empty_end_page_and_error_page(self):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        parser = ListingParser(plan.listing['parser'], adapter=adapter)
        empty = '<html><body><main><h2 class="results__title">0 résultat</h2></main></body></html>'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, empty, LIST_URL + '?page=257'))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(ValueError):
            self.crawler._enumeration_items('<html>maintenance</html>', LIST_URL + '?page=3')
        still_next = empty.replace('</main>', '<ul><li class="pager__item--next"><a href="?page=4">s</a></li></ul></main>')
        with self.assertRaises(ValueError):
            self.crawler._enumeration_items(still_next, LIST_URL + '?page=3')

    def test_detail_paper_equals_crawl_paper(self):
        items = self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL)
        item = next(i for i in items if i['url'] == self.fx['detail_url'])
        calls = []

        def transport(url):
            calls.append(url)
            return self.fx['detail_html']
        with patch.object(self.crawler, '_curl_get', transport), patch('time.sleep', lambda *a: None):
            expected = self.crawler._paper(copy.deepcopy(item))
        self.assertEqual([item['url']], calls)
        with guarded():
            paper = self.crawler._enumeration_detail(self.fx['detail_html'], copy.deepcopy(item), item['url'])
        self.assertEqual(expected, paper)
        self.assertEqual(item['pdf_href'], paper_identity(clean_paper(paper))[0])
        self.assertNotIn('_curl_get', vars(self.crawler))

    def test_direct_pdf_card_uses_listing_fields_only(self):
        item = dict(self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL)[0])
        item.update(url='https://www.caissedesdepots.fr/sites/cdc.fr/files/a/cp.pdf', pdf_href='/sites/cdc.fr/files/a/cp.pdf',
                    original_filename='cp.pdf', summary='Résumé du communiqué ' * 5)
        with guarded():
            paper = self.crawler._enumeration_detail('%PDF-1.4 binary', item, item['url'])
        self.assertEqual(item['summary'], paper['abstract'])
        self.assertEqual('/sites/cdc.fr/files/a/cp.pdf', paper['external_id'])

    def test_original_methods_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
