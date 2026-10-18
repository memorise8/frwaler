"""college-de-france-fr-en: current card markup is parsed, the older markup is kept,
and enumeration entry points give the same Paper as the unchanged crawl() loop. Memory only."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

ROOT = Path(__file__).resolve().parents[1]
SITE = 'college-de-france-fr-en'
LIST_URL = 'https://www.college-de-france.fr/en/news'
PLAN = {'version': 1, 'sites': {SITE: {
    'strategy': 'last_page_backward',
    'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                'detail_method': '_enumeration_detail', 'detail_args': ['html', 'item', 'url']},
    'listing': {'url': LIST_URL, 'params': {'f[0]': 'type:4825'}, 'page_param': 'page', 'start_page': 0,
                'parser': {'type': 'adapter', 'key_field': 'url', 'url_field': 'url', 'title_field': 'title',
                           'date_field': 'listed_date', 'listing_marker': '.view-id-search_news'}},
    'last_page': {'overlap_pages': 1}}}}
OLD_CARD = ('<html><body><a class="card-news" href="/en/news/old-item"><div class="card-news__title">Old item</div>'
            '<div class="card-news__date">Published on 3 March 2021</div>'
            '<div class="card-news__author">Chair of Physics</div></a></body></html>')


class CollegeDeFranceSplit(unittest.TestCase):
    def setUp(self):
        self.fx = json.loads((ROOT / 'tests/fixtures/l2b_g2' / (SITE + '.json')).read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child',
                     'sqlite3.connect'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)
        self.crawler, _ = load(SITE)
        self.crawler.detail_delay = 0
        self.crawler._last_save_created = True

    def test_current_and_older_card_markup(self):
        url = LIST_URL + '?f%5B0%5D=type%3A4825&page=0'
        rows = self.crawler._enumeration_items(self.fx['listing_html'], url)
        self.assertEqual(2, len(rows))
        self.assertTrue(all(r['title'] and r['listed_date'] and '/en/news/' in r['url'] for r in rows))
        self.assertEqual(self.crawler._list_url(0), rows[0]['list_url'])
        old = self.crawler._enumeration_items(OLD_CARD, LIST_URL + '?f%5B0%5D=type%3A4825&page=2')
        self.assertEqual([{'url': 'https://www.college-de-france.fr/en/news/old-item', 'title': 'Old item',
                           'listed_date_raw': 'Published on 3 March 2021', 'listed_date': '2021-03-03',
                           'list_label': 'Chair of Physics', 'image': None, 'list_page': 2,
                           'list_url': self.crawler._list_url(2)}], old)

    def test_papers_equal_crawl(self):
        captured = []

        def transport(url, context='request', referer=None, accept=None):
            return self.fx['detail_html'] if '/en/news/' in url else self.fx['listing_html']
        with patch.object(self.crawler, '_curl_get', transport), \
                patch.object(self.crawler, '_save_paper', lambda p: captured.append(copy.deepcopy(p))), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            self.crawler.crawl(limit=10)
        self.assertEqual(2, len(captured))
        plan = site_plan(PLAN, SITE, registered=[SITE])
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        page = ListingParser(plan.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, self.fx['listing_html'], LIST_URL + '?f%5B0%5D=type%3A4825&page=0'))
        with guarded(), contextlib.redirect_stdout(io.StringIO()):
            papers = [self.crawler._enumeration_detail(self.fx['detail_html'], dict(i.payload), i.url) for i in page.items]
        self.assertEqual(captured, papers)

    def test_short_abstract_skip_is_preserved(self):
        rows = self.crawler._enumeration_items(self.fx['listing_html'], LIST_URL)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(self.crawler._enumeration_detail('<html><body><h1>T</h1></body></html>', rows[0]))

    def test_empty_end_page_is_explicit(self):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        parser = ListingParser(plan.listing['parser'], adapter=adapter)
        empty = '<html><body><div class="view view-id-search_news"><nav class="pager"></nav></div></body></html>'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, empty, LIST_URL + '?page=3'))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', LIST_URL + '?page=1'))
        self.assertTrue(err.extra.get('parse_error'))

    def test_other_original_methods_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
