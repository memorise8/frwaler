"""L3b-20261018: enumeration split for kommunekredit-com-about-us.
The single reports page carries DownloadList JSON modules (no next link, no
detail pages). crawl() reads the PDF response headers with curl -I; the split
reads the same headers from the framework's GET of the PDF (detail_args resp).
external_id (the /media/ path) comes from the list row and is unchanged."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load, run_split

SITE = 'kommunekredit-com-about-us'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'
LAST_MODIFIED = {
    'https://www.kommunekredit.com/media/aaaa1111/synthetic_ar_2031.pdf': 'Mon, 10 Mar 2031 08:15:00 GMT',
    'https://www.kommunekredit.com/media/bbbb2222/synthetic_ir_2030.pdf': None,
}
MODULE_HELPERS = {
    '_external_id_from_url': 'ce8733e0c222ccdceecddda3e22f85a74ade6fcedd54bcf244aeb23a3a1fed80',
    '_parse_download_items': '5f006637a4a912b180c2706f5a8915e3e1e5bf56a7dbbbd2919405f3dfa64c3c',
    '_enrich_with_pdf_headers': '13ebf77d6bb630a62f7a882b4e45084c0dcf90a3fd9c2514cae05356e6838b99',
    '_paper_from_item': '3c7a37eb0f1d313b2f2c4cae85b7d21d785e8c2a2337afccf996ba7524fdf5e4',
}


def _comparable(paper):
    """The fields crawl() stores, with the raw header dump (curl -I text vs the
    GET header lines) removed from metadata: everything else must be equal."""
    paper = dict(paper)
    meta = json.loads(paper.pop('metadata'))
    meta.pop('raw_headers', None)
    meta['headers'] = {k: v for k, v in meta.get('headers', {}).items() if k in ('last-modified', 'content-disposition')}
    paper['metadata'] = meta
    return paper


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured, calls = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def text(url, **kw):
            calls.append(url)
            return fixture['listing_body'] if len(calls) == 1 else None

        def heads(url, **kw):
            lm = LAST_MODIFIED.get(url)
            return 'HTTP/1.1 200 OK\nContent-Type: application/pdf\n' + (f'Last-Modified: {lm}\n' if lm else '')

        with patch.object(module, '_curl_text', text), patch.object(module, '_curl_headers', heads), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def pipeline(self, fixture=None, redirect=False):
        fixture = fixture or self.fixture
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        response = Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], p.listing['url'])
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(response).items
        self.assertEqual(2, len(items))
        bare = build_url(p.listing['url'], {}, drop={'page'})
        self.assertEqual([i.key for i in items], [i.key for i in ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], bare)).items])
        self.raw_papers = []

        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            headers = {'Content-Type': 'application/pdf'}
            if LAST_MODIFIED.get(url):
                headers['Last-Modified'] = LAST_MODIFIED[url]
            return Response(200, headers, fixture['details'][url], url + '?moved=1' if redirect else url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
                self.raw_papers.append(copy.deepcopy(paper))
        return papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        actual = self.pipeline()
        self.assertEqual([_comparable(clean_paper(p)) for p in expected], [_comparable(p) for p in actual])
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(['aaaa1111/synthetic_ar_2031.pdf', 'bbbb2222/synthetic_ir_2030.pdf'],
                         [p['external_id'] for p in actual])
        # Last-Modified wins for post_number/date; without it the title year rule applies.
        self.assertEqual(['20310310081500', '2030'], [p['post_number'] for p in actual])
        self.assertEqual(['2031-03-10', '2030-06-30'], [p['published_date'] for p in actual])

    def test_detail_redirect_preserves_registered_identity(self):
        expected, _ = self.native()
        self.assertEqual([_comparable(clean_paper(p)) for p in expected],
                         [_comparable(p) for p in self.pipeline(redirect=True)])

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is a template ('<title> is a KommuneKredit report listed in ...'): never short.
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        self.assertEqual(2, len(self.pipeline()))

    def test_module_helpers_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        tree = ast.parse(code)
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name, digest in MODULE_HELPERS.items():
            self.assertEqual(digest, actual[name], name)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        page2 = build_url(p.listing['url'], {'page': 2})
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], page2))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, page2))
        self.assertEqual('listing_structure_missing', foreign.extra.get('parse_error'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))
        for url in (p.listing['url'], build_url(p.listing['url'], {'page': 1})):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_row_url_matches_crawl_for_page_one_and_bare_url(self):
        crawler, _ = load(SITE)
        base = 'https://www.kommunekredit.com/about-us/reports-numbers-and-publications/reports/'
        for url in (base, base + '?page=1'):
            rows = crawler._enumeration_items(self.fixture['listing_body'], url)
            self.assertEqual([base + '#synthetic'] * 2, [r['url'] for r in rows])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        same_page = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url)
        result, docs = run_split(self.SITE, end=same_page)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
