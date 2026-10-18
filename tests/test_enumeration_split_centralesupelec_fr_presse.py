"""centralesupelec-fr-presse: enumeration entry points give the same Paper (node_id
post_number, final detail URL) as the unchanged crawl(). Memory only."""
import ast
import contextlib
import copy
import hashlib
import io
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
SITE = 'centralesupelec-fr-presse'
LIST_URL = 'https://www.centralesupelec.fr/presse/communiques'
PLAN = {'version': 1, 'sites': {SITE: {
    'strategy': 'last_page_backward',
    'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                'detail_method': '_enumeration_detail', 'detail_args': ['html', 'item', 'url']},
    'listing': {'url': LIST_URL, 'page_param': 'page', 'start_page': 0,
                'parser': {'type': 'adapter', 'key_field': 'node_id', 'url_field': 'url', 'title_field': 'title',
                           'date_field': 'published_date', 'listing_marker': 'article[data-history-node-id]'}},
    'last_page': {'overlap_pages': 1}}}}
EMPTY = '<html><body><article data-history-node-id="1"><h1>Communiqués</h1></article></body></html>'


class CentraleSupelecSplit(unittest.TestCase):
    def setUp(self):
        self.fx = json.loads((ROOT / 'tests/fixtures/l2b_g2' / (SITE + '.json')).read_text())
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child',
                     'sqlite3.connect'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)
        self.crawler, _ = load(SITE)
        c = self.crawler
        c._delay = 0
        c.delivery_should_cancel = lambda: False
        c._report_source_failure = lambda reason: None
        c._last_save_created = True

    def native(self, detail_html):
        captured = []

        def transport(url, follow=False, timeout=30):
            if url.startswith(self.crawler.base_url + '/node/'):
                return detail_html, self.fx['detail_final_url'] if url.endswith('299767') else url
            return (self.fx['listing_html'] if url.endswith('page=0') else EMPTY), url
        with patch.object(self.crawler, '_curl_get', transport), \
                patch.object(self.crawler, '_save_paper', lambda p: captured.append(copy.deepcopy(p))), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            self.crawler.crawl(limit=10)
        return captured

    def pipeline(self, detail_html):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        page = ListingParser(plan.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, self.fx['listing_html'], LIST_URL + '?page=0'))
        self.assertEqual(2, len(page.items))
        papers = []
        for item in page.items:
            final = self.fx['detail_final_url'] if item.url.endswith('299767') else item.url
            with guarded():
                paper = self.crawler._enumeration_detail(detail_html, dict(item.payload), final)
            if paper is not None:
                papers.append(paper)
        return papers

    def test_papers_and_identity_equal_crawl(self):
        expected = self.native(self.fx['detail_html'])
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.fx['detail_html'])
        self.assertEqual(expected, actual)
        self.assertEqual('299767', paper_identity(clean_paper(actual[0]))[0])
        self.assertEqual(self.fx['detail_final_url'], paper_identity(clean_paper(actual[0]))[1])
        self.assertNotIn('_curl_get', vars(self.crawler))

    def test_skip_rule_preserved(self):
        listing = self.fx['listing_html']
        try:
            self.fx['listing_html'] = listing.replace('.pdf', '.html')
            bare = '<html><body><article data-history-node-id="299767"></article></body></html>'
            expected = self.native(bare)
            actual = self.pipeline(bare)
        finally:
            self.fx['listing_html'] = listing
        self.assertEqual(expected, actual)

    def test_empty_end_page_is_explicit(self):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        parser = ListingParser(plan.listing['parser'], adapter=adapter)
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, LIST_URL + '?page=21'))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', LIST_URL + '?page=2'))
        self.assertTrue(err.extra.get('parse_error'))

    def test_original_methods_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


if __name__ == '__main__':
    unittest.main()
