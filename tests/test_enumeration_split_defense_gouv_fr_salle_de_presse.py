"""L2a (2026-10-18): enumeration methods for defense-gouv-fr-salle-de-presse.
The listing card is the whole record; the enumeration paper must equal what the
unchanged crawl() saves (external_id = PDF file name). Offline: synthetic HTML."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'defense-gouv-fr-salle-de-presse'
BASE = 'https://www.defense.gouv.fr'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1, 'list_method': '_enumeration_items',
                        'list_args': ['html'], 'paper_method': '_enumeration_paper', 'paper_args': ['item']},
            'listing': {'url': BASE + '/salle-de-presse', 'params': {'page': 0}, 'page_param': 'page',
                        'start_page': 0,
                        'parser': {'type': 'adapter', 'key_field': 'external_id', 'url_field': 'pdf_url',
                                   'title_field': 'actual_title', 'date_field': 'published_date',
                                   'listing_marker': 'main'}},
            'last_page': {'overlap_pages': 1}}}}


def card(date_title, tag, date, pdf):
    link = f'<a href="http://www.defense.gouv.fr/sites/default/files/ministere-armees/{pdf}">PDF</a>' if pdf else ''
    return (f'<article class="fr-card"><h2 class="fr-card__title">{date_title}</h2><p class="fr-tag">{tag}</p>'
            f'<p class="fr-card__document-text">{date}</p>{link}</article>')


LISTING = ('<html><body><main>'
           + card('16.10.2026 Communiqué : Transfert du sous-marin', 'Communiqué', '16 octobre 2026',
                  'Communiqu%C3%A9_A_transfert%20du%20sous-marin.pdf')
           + card('15.10.2026 Discours : Cérémonie', 'Discours', '15 octobre 2026', None)
           + '</main></body></html>')
EMPTY = '<html><body><main><p>Aucun résultat</p></main></body></html>'


class Enumeration(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured, calls = [], []

        def transport(url):
            calls.append(url)
            return LISTING if len(calls) == 1 else EMPTY
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['Communiqué_A_transfert du sous-marin', 'Cérémonie'],
                         [p['external_id'] for p in expected])
        p, adapter = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, LISTING, BASE + '/salle-de-presse?page=0')).items
        actual = [adapter.materialize(i, None) for i in items]
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in actual])

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = BASE + '/salle-de-presse?page=999'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
