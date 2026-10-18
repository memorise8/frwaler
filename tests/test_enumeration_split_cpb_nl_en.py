"""L2a (2026-10-18): enumeration methods for cpb-nl-en.
The enumeration path must produce the paper the unchanged crawl() saves
(external_id = node id or slug, url = og:url). Offline: synthetic HTML."""
import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'cpb-nl-en'
BASE = 'https://www.cpb.nl'
MARKER = '[data-component-id="cpb:faceted_list"]'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'item']},
            'listing': {'url': BASE + '/en/publications', 'params': {'page': 0},
                        'page_param': 'page', 'start_page': 0,
                        'parser': {'type': 'adapter', 'url_field': 'detail_url', 'title_field': 'title',
                                   'date_field': 'listed_date', 'listing_marker': MARKER}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Synthetic CPB policy analysis résumé with Korean 자료 for an offline equality test. ' * 3


def row(path, title, date):
    return (f'<div data-component-id="cpb:overview_list_item"><a class="overview-list-item__link" href="{path}">'
            f'<h2 class="overview-list-item__heading">{title}</h2></a>'
            f'<p class="overview-list-item__subheading">{date}</p>'
            f'<p class="overview-list-item__body-text">Teaser</p><span class="overview-list-item__tag">Forecast</span></div>')


LISTING = ('<html><body><div data-component-id="cpb:faceted_list">'
           + row('/en/forecast/one', 'One', 'June 30, 2026') + row('/en/publication/two', 'Two', 'May 2, 2025')
           + '</div><li class="pager__item--next"><a href="?page=1">next</a></li></body></html>')
EMPTY = '<html><body><div data-component-id="cpb:faceted_list"><p>No results</p></div></body></html>'
URLS = [BASE + '/en/forecast/one', BASE + '/en/publication/two']


def detail(url, title, nid):
    node = f' data-history-node-id="{nid}"' if nid else ''
    return (f'<html><head><meta name="og:url" content="{url}"><meta name="publicationdatetime" '
            f'content="2026-06-30T09:00:00Z"><meta name="author" content="A. Author, B. Author">'
            f'<meta name="contenttype" content="Forecast"></head><body><article{node}>'
            f'<h1 class="title-block__title">{title}</h1><div class="page-introduction__text"><p>{LONG}</p></div>'
            f'<section aria-label="Downloads"><a href="/system/files/{title}.pdf" download>PDF</a></section>'
            f'</article></body></html>')


DETAILS = {URLS[0]: detail(URLS[0], 'One', 4908), URLS[1]: detail(URLS[1], 'Two', None)}  # second: slug


class Enumeration(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, details=DETAILS):
        crawler, _ = load(SITE)
        captured, listing_calls = [], []

        def transport(url, referer=None):
            if url in details:
                return details[url]
            listing_calls.append(url)
            return LISTING if len(listing_calls) == 1 else EMPTY
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler), crawler

    def pipeline(self, details=DETAILS):
        p, adapter, crawler = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, LISTING, BASE + '/en/publications?page=0')).items
        self.assertEqual(URLS, [i.url for i in items])
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, details[u], u)
        with contextlib.redirect_stdout(io.StringIO()):
            papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        self.assertNotIn('_curl_get', crawler.__dict__)
        return [x for x in papers if x is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['4908', 'two'], [p['external_id'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        short = {u: v.replace(LONG, 'Short.') for u, v in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.pipeline(short))

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter, _ = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = BASE + '/en/publications?page=236'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
