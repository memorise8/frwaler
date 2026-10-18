"""L2a (2026-10-18): enumeration methods for afd-fr-fr.
The listing rows and the detail paper of the enumeration plan must equal what crawl()
saves (same external_id/url). Offline: synthetic HTML, no network/process/database."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'afd-fr-fr'
BASE = 'https://www.afd.fr'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html'],
                        'detail_method': '_enumeration_detail_paper', 'detail_args': ['html', 'url']},
            'listing': {'url': BASE + '/fr/publications/liste',
                        'params': {'words': '', 'type[170]': '170', 'page': 0},
                        'page_param': 'page', 'start_page': 0,
                        'parser': {'type': 'adapter', 'url_field': 'url', 'title_field': 'title',
                                   'listing_marker': '#views-exposed-form-publication-publication-list'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Synthèse de recherche synthétique avec 자료 coréen pour un test hors ligne. ' * 4
FORM = '<form id="views-exposed-form-publication-publication-list"></form>'


def card(slug, title):
    return (f'<div class="views-row"><h3 class="fr-card__title"><a href="/fr/ressources/{slug}" '
            f'class="fr-card__link">{title}</a></h3></div>')


LISTING = (f'<html><body>{FORM}<div class="fr-cards-grid--list">' + card('etude-un', 'Étude un')
           + card('etude-deux', 'Étude deux') + card('etude-un', 'Étude un') + '</div></body></html>')
EMPTY = f'<html><body>{FORM}<div class="my-content"><p>Aucun contenu trouvé</p></div></body></html>'


def detail(n, with_nid):
    layer = {'content_title': f'Étude {n}', 'content_field_publication_date': f'2025-0{n}-0{n}',
             'content_field_resource_type': 'Document de recherche', 'content_field_collection': 'Papiers',
             'content_field_download': f'etude{n}.pdf', 'content_field_metatags': json.dumps({'keywords': 'climat'})}
    if with_nid:
        layer['content_nid'] = 1000 + n
    settings = json.dumps({'pianoAnalytics': {'dataLayer': layer}})
    return (f'<html><head><meta name="description" content="{LONG}">'
            f'<script type="application/json" data-drupal-selector="drupal-settings-json">{settings}</script>'
            f'</head><body><main><a href="/sites/default/files/etude{n}.pdf">PDF</a></main></body></html>')


URLS = [BASE + '/fr/ressources/etude-un', BASE + '/fr/ressources/etude-deux']
DETAILS = {URLS[0]: detail(1, True), URLS[1]: detail(2, False)}  # second: slug fallback


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
        crawler._source_failure = None
        crawler._report_source_failure = lambda *a: None
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
        url = BASE + '/fr/publications/liste?words=&type%5B170%5D=170&page=0'
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, LISTING, url)).items
        self.assertEqual(URLS, [i.url for i in items])
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, details[u], u)
        with contextlib.redirect_stdout(io.StringIO()):
            papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        return [x for x in papers if x is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected))
        self.assertEqual(['1001', 'etude-deux'], [p['external_id'] for p in expected])
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
        url = BASE + '/fr/publications/liste?page=39'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
