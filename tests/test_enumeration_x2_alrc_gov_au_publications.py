"""L3b X2 (ext:multi_listing): alrc-gov-au-publications reads both delivered lists in one plan.
Explicit segments: 'final-report' (/publications/final-report/page/N/) and 'delivered-search'
(/page/N/?s=pdf&type=all, its own url/first_page_url). The list method takes the scope from the
list URL and validates the publisher pager against alrc_scope.list_url(scope, page), exactly as
_alrc_list() does; the paper is _alrc_paper() for that scope. crawl() is unchanged. No network."""
import contextlib
import copy
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.alrc_scope import list_url
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load, split_fixture

SITE = 'alrc-gov-au-publications'
ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.x2.json')).read_text())['sites'][SITE]
EMPTY = ('<html><body><main class="content"><section class="section-listing posts">'
         '<div class="section-listing-inner"></div></section></main></body></html>')
NEWS = 'https://www.alrc.gov.au/news/synthetic-news-1/'


def search_page(page, last, urls):
    rows = ''.join(f'<div class="section-listing-item"><h2><a href="{u}">Synthetic {i}</a></h2>'
                   f'<span class="section-listing-item-date">0{i}.01.2031</span>'
                   f'<span class="section-listing-item-type">News</span><p>Snippet {i}.</p></div>'
                   for i, u in enumerate(urls, 1))
    pages = ''.join(f'<span class="page current">{n}</span>' if n == page else
                    f'<a class="page" href="{list_url("delivered-search", n)}">{n}</a>' for n in range(1, last + 1))
    nxt = f'<a class="next" href="{list_url("delivered-search", page + 1)}">Next</a>' if page < last else ''
    return (f'<html><body><main class="content"><section class="section-listing posts">{rows}</section>'
            f'<div class="tw-pagination">{pages}{nxt}</div></main></body></html>')


class X2MultiListing(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        detail = self.fixture['details']['https://www.alrc.gov.au/publication/synthetic-report-8/']
        self.details = dict(self.fixture['details'])
        self.details[NEWS] = (detail.replace('https://www.alrc.gov.au/publication/synthetic-report-8/', NEWS)
                              .replace('?p=90008', '?p=90101').replace('postid-90008', 'postid-90101'))
        self.search = {1: search_page(1, 2, [NEWS, 'https://www.alrc.gov.au/publication/synthetic-report-9/']),
                       2: search_page(2, 2, ['https://www.alrc.gov.au/publication/synthetic-report-8/'])}
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('io')
            raise AssertionError('attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def test_scope_comes_from_the_list_url(self):
        crawler, _ = load(SITE)
        self.assertEqual('delivered-search', crawler._enumeration_scope('https://www.alrc.gov.au/?s=pdf&type=all'))
        self.assertEqual('delivered-search', crawler._enumeration_scope('https://www.alrc.gov.au/page/7/?s=pdf&type=all'))
        self.assertEqual('final-report', crawler._enumeration_scope('https://www.alrc.gov.au/publications/final-report/page/2/'))
        self.assertEqual('final-report', crawler._enumeration_scope('https://www.alrc.gov.au/publications/final-report/?paged=3'))

    def test_search_rows_and_paper_equal_the_crawlers_own_conversion(self):
        crawler, _ = load(SITE)
        native, _ = load(SITE)
        native._alrc_fetch = lambda url: self.search[1] if url == list_url('delivered-search', 1) else self.details[url]
        items, last = native._alrc_list('delivered-search', 1)
        self.assertEqual(2, last)
        rows = crawler._enumeration_items(self.search[1], 'https://www.alrc.gov.au/?s=pdf&type=all')
        self.assertEqual([dict(i, source_list_url=list_url('delivered-search', 1), source_scope='delivered-search')
                          for i in items], rows)
        for item, row in zip(items, rows):
            expected = native._alrc_paper(copy.deepcopy(item), 'delivered-search', list_url('delivered-search', 1))
            self.assertEqual(expected, crawler._enumeration_paper(self.details[row['url']], row))
        self.assertEqual('90101', rows and crawler._enumeration_paper(self.details[NEWS], rows[0])['external_id'])
        # The search pager checked against the final-report list (or the wrong page) is an error.
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(self.search[1], 'https://www.alrc.gov.au/publications/final-report/')
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(self.search[1], 'https://www.alrc.gov.au/page/2/?s=pdf&type=all')

    def test_final_report_rows_are_unchanged(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'], 'https://www.alrc.gov.au/publications/final-report/')
        self.assertTrue(rows and all('source_scope' not in r for r in rows))
        self.assertEqual({list_url('final-report', 1)}, {r['source_list_url'] for r in rows})

    def test_plan_reads_both_lists_end_to_end(self):
        from tests.enumeration_fakes import Env
        site_plan({'version': 1, 'sites': {SITE: PLAN}}, SITE, registered=[SITE])
        plan = copy.deepcopy(PLAN)
        plan['request_interval_s'] = 0
        urls = []

        def handler(url):
            urls.append(url)
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in self.details:
                return Response(200, {'Content-Type': 'text/html'}, self.details[url], url)
            if url == 'https://www.alrc.gov.au/publications/final-report/':
                return Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url)
            for n, body in self.search.items():
                if url == list_url('delivered-search', n):
                    return Response(200, {'Content-Type': 'text/html'}, body, url)
            if url.startswith('https://www.alrc.gov.au/publications/final-report/page/'):
                return Response(200, {'Content-Type': 'text/html'}, EMPTY, url)
            return Response(404, {'Content-Type': 'text/html'}, '<html>Not found</html>', url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['90008', '90009', '90101'], sorted(d['post_number'] for d in docs))
        self.assertIn('https://www.alrc.gov.au/?s=pdf&type=all', urls)
        self.assertIn('https://www.alrc.gov.au/page/2/?s=pdf&type=all', urls)
        self.assertNotIn('https://www.alrc.gov.au/publications/delivered-search/', urls)


if __name__ == '__main__':
    unittest.main()
