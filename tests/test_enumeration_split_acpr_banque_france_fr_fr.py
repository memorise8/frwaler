"""acpr-banque-france-fr-fr: enumeration entry points reuse crawl()'s parsers unchanged.
Memory only: no network, process or database I/O (fixture is a small excerpt of the live pages)."""
import ast
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
SITE = 'acpr-banque-france-fr-fr'
LIST_URL = 'https://acpr.banque-france.fr/fr/communiques-de-presse'
PLAN = {'version': 1, 'sites': {SITE: {
    'strategy': 'last_page_backward',
    'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                'detail_method': '_enumeration_detail', 'detail_args': ['html', 'item', 'url']},
    'listing': {'url': LIST_URL, 'page_param': 'page', 'start_page': 0,
                'parser': {'type': 'adapter', 'key_field': 'slug', 'url_field': 'url', 'title_field': 'title',
                           'listing_marker': '.view-id-espaces2_press_release'}},
    'last_page': {'overlap_pages': 1}}}}


class AcprSplit(unittest.TestCase):
    def setUp(self):
        self.fx = json.loads((ROOT / 'tests/fixtures/l2b_g2' / (SITE + '.json')).read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child',
                     'sqlite3.connect'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)
        self.crawler, self.module = load(SITE)

    def adapter(self):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        return plan, Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)

    def test_items_equal_crawl_listing(self):
        expected = self.module._listing(self.fx['listing_html'], 'press', 0)['items']
        self.assertEqual(2, len(expected))
        with guarded():
            rows = self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL + '?page=0')
            bare = self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL)
        self.assertEqual([dict(x, scope='press') for x in expected], rows)
        self.assertEqual(rows, bare)

    def test_empty_page_after_last_and_error_page(self):
        plan, adapter = self.adapter()
        parser = ListingParser(plan.listing['parser'], adapter=adapter)
        empty = ('<html><body><div class="view view-id-espaces2_press_release"><div class="view-empty">'
                 'Aucun résultat</div></div></body></html>')
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, empty, LIST_URL + '?page=42'))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(Exception):
            with guarded():
                self.crawler._enumeration_items('<html>maintenance</html>', LIST_URL + '?page=3')
        with self.assertRaises(ValueError):
            self.crawler._enumeration_items(self.fx['listing_html'], 'https://acpr.banque-france.fr/fr/autre')

    def test_detail_paper_and_identity_equal_crawl(self):
        rows = self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL)
        item = next(r for r in rows if r['url'] == self.fx['detail_url'])
        legacy = {k: v for k, v in item.items() if k != 'scope'}
        expected = self.crawler._parse_detail(self.fx['detail_html'], legacy)
        with guarded():
            paper = self.crawler._enumeration_detail(self.fx['detail_html'], item, item['url'])
        self.assertEqual(expected, paper)
        self.assertEqual(item['slug'], paper_identity(clean_paper(paper))[0])
        self.assertEqual(item['url'], paper_identity(clean_paper(paper))[1])

    def test_pdf_only_notice_fails_without_download(self):
        rows = self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL)
        item = next(r for r in rows if r['url'] == self.fx['detail_url'])
        html = ('<html><body><h1>%s</h1><article class="node"><a href="/system/files/x.pdf">PDF</a>'
                '</article></body></html>' % item['title'])
        with guarded():
            with self.assertRaises(ValueError):
                self.crawler._enumeration_detail(html, item, item['url'])
        self.assertNotIn('_abstract_from_pdf', vars(self.crawler))

    def test_original_methods_unchanged(self):
        path = ROOT / 'crawler/sites/custom' / (SITE + '.py')
        code = path.read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
