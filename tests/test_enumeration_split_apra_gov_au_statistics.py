"""L2a (2026-10-18): enumeration methods for apra-gov-au-statistics.
The enumeration listing rows + detail paper must equal what the unchanged crawl()
saves (external_id = slug, url). Offline: synthetic HTML, no network/process/DB."""
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

SITE = 'apra-gov-au-statistics'
BASE = 'https://www.apra.gov.au'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'item']},
            'listing': {'url': BASE + '/news-and-publications',
                        'params': {'created': 'All', 'document_type[0]': 'bundle:statistical_publication',
                                   'sort_bef_combine': 'created_DESC', 'page': 0},
                        'page_param': 'page', 'start_page': 0,
                        'parser': {'type': 'adapter', 'url_field': 'url', 'title_field': 'link_text',
                                   'date_field': 'listed_date', 'listing_marker': 'form.views-exposed-form'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Synthetic quarterly statistics résumé with Korean 자료 for an offline equality test. ' * 3
FORM = '<form class="views-exposed-form"></form>'


def row(slug, title, date):
    return (f'<div class="views-row"><h3 class="search-result__title"><a href="/news-and-publications/{slug}">'
            f'{title}</a></h3><span class="anx-pill--split"><span class="anx-pill__label-first">Published</span>'
            f'<span class="anx-pill__label-last">{date}</span></span>'
            f'<span class="tag-bungle-type">Statistical publication</span></div>')


LISTING = (f'<html><body>{FORM}' + row('series-one', 'Series one', '31 July 2026')
           + row('series-two', 'Series two', '2 May 2025') + row('series-one', 'Series one', '31 July 2026')
           + '</body></html>')
EMPTY = f'<html><body>{FORM}<p>No results</p></body></html>'
URLS = [BASE + '/news-and-publications/series-one', BASE + '/news-and-publications/series-two']
DETAILS = {
    URLS[0]: f'<html><body><main><h1>Series one</h1><time datetime="2026-07-31T10:00:00+10:00">x</time>'
             f'<p>{LONG}</p><a href="/system/files/Series%20One.pdf">PDF</a></main></body></html>',
    URLS[1]: f'<html><body><main><h1>Series two</h1><p>{LONG}</p></main></body></html>',  # no <time>
}


class Enumeration(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, details=DETAILS):
        crawler, module = load(SITE)
        captured, listing_calls = [], []

        def transport(url, *a, **k):
            if url in details:
                return details[url]
            listing_calls.append(url)
            return LISTING if len(listing_calls) == 1 else EMPTY
        crawler._last_save_created = True
        crawler._source_failure = None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, details=DETAILS):
        p, adapter = self.adapter()
        url = BASE + '/news-and-publications?page=0'
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, LISTING, url)).items
        self.assertEqual(URLS, [i.url for i in items])
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, details[u], u)
        papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        return [x for x in papers if x is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['series-one', 'series-two'], [p['external_id'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        short = {u: v.replace(LONG, 'Short.') for u, v in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.pipeline(short))

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = BASE + '/news-and-publications?page=7'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
