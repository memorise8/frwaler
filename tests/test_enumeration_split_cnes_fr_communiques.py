"""L2a (2026-10-18): enumeration methods for cnes-fr-communiques.
The enumeration path must produce the paper the unchanged crawl() saves
(external_id = slug, url). Offline: synthetic HTML, no network/process/DB."""
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

SITE = 'cnes-fr-communiques'
BASE = 'https://cnes.fr'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'item']},
            'listing': {'url': BASE + '/communiques', 'params': {'display_parameters[display_type]': '0', 'page': 0},
                        'page_param': 'page', 'start_page': 0,
                        'parser': {'type': 'adapter', 'key_field': 'slug', 'url_field': 'url', 'title_field': 'title',
                                   'date_field': 'listed_date', 'listing_marker': 'main'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Communiqué synthétique du CNES avec 자료 coréen pour un test hors ligne. ' * 4


def card(slug, title, date, tags):
    t = ''.join(f'<p class="fr-tag">{x}</p>' for x in tags)
    return (f'<article class="fr-card"><h3><a class="fr-card__link" href="/communiques/{slug}">{title}</a></h3>'
            f'<p class="fr-card__detail">{date}</p>{t}</article>')


LISTING = ('<html><body><main>' + card('lancement-un', 'Lancement un', '07 mai 2026', ['Lanceurs', 'Ariane'])
           + card('mission-deux', 'Mission deux', '12 février 2025', [])
           + '<article class="fr-card"><a class="fr-card__link" href="/actualites/x">x</a></article></main></body></html>')
EMPTY = '<html><body><main><p>Aucun résultat</p></main></body></html>'
URLS = [BASE + '/communiques/lancement-un', BASE + '/communiques/mission-deux']
DETAILS = {
    URLS[0]: f'<html><body><nav>menu</nav><main><p>Publié le 8 mai 2026</p><p>{LONG}</p>'
             f'<a href="/sites/default/files/CP01.pdf">CP</a></main></body></html>',
    URLS[1]: f'<html><body><main><p>{LONG}</p></main></body></html>',  # no "Publié le": listed date
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
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, details=DETAILS):
        p, adapter = self.adapter()
        url = BASE + '/communiques?display_parameters%5Bdisplay_type%5D=0&page=0'
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, LISTING, url)).items
        self.assertEqual(URLS, [i.url for i in items])
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, details[u], u)
        papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        return [x for x in papers if x is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['lancement-un', 'mission-deux'], [p['external_id'] for p in expected])
        self.assertEqual(['2026-05-08', '2025-02-12'], [p['published_date'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        short = {u: v.replace(LONG, 'Court.') for u, v in DETAILS.items()}
        self.assertEqual([], self.native(short))
        self.assertEqual([], self.pipeline(short))

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = BASE + '/communiques?page=14'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
