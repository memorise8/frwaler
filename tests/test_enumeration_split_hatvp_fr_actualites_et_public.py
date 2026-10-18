"""ND-3 (2026-10-19): enumeration split for hatvp-fr-actualites-et-public.
The plan reads the listing's own Communiqués filter; _enumeration_items/_enumeration_paper
must give crawl()'s papers (crawl() itself unchanged). Offline: synthetic pages."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'hatvp-fr-actualites-et-public'
BASE = 'https://www.hatvp.fr'
LIST1 = BASE + '/actualites-et-publications/'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1, 'none_is_rule_skip': True,
                        'list_method': '_enumeration_items', 'list_args': ['html'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'item']},
            'listing': {'url': BASE + '/actualites-et-publications/page/{page}/', 'first_page_url': LIST1,
                        'params': {'anchor': 'communiques', 'type': 'communiques'}, 'start_page': 1,
                        'parser': {'type': 'adapter', 'key_field': 'external_id', 'url_field': 'url',
                                   'date_field': 'published_date', 'title_field': 'title',
                                   'listing_marker': '#communiques'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'La Haute Autorité publie un communiqué synthétique pour un test hors ligne du découpage. '
ROWS = [('premier-communique', 'Communiqués', '28 mai 2026', LONG * 3, True),
        ('une-tribune', 'Tribunes', '20 mai 2026', LONG * 3, False),
        ('communique-court', 'Communiqués', '5 janvier 2026', 'Trop court.', False)]


def li(slug, tag, date, text):
    return (f'<li><div class=wrapper><div class="grid grid--10"><h4> <a href={BASE}/presse/{slug}/ > '
            f'{slug.replace("-", " ").title()} </a></h4><div class=text><p><p>{text[:60]}</p></div>'
            f'<span class=tag>{tag}</span><time>{date}</time></div></div></li>')


def listing(rows):
    return ('<html><body><div id=actualites></div><div id=communiques style="display: none;">'
            '<ul class="articles articles-float">' + ''.join(li(s, t, d, x) for s, t, d, x, _ in rows)
            + '</ul></div></body></html>')


PAGE = listing(ROWS)
EMPTY = listing([])
PDF = BASE + '/wordpress/wp-content/uploads/2026/05/Rapport.pdf'
DETAILS = {f'{BASE}/presse/{s}/': (f'<html><body><article><header><h1>{s.title()}</h1><time>{d}</time></header>'
                                   f'<div class=content><p>{x}</p>{f"<a href={PDF}>pdf</a>" if pdf else ""}</div></div>'
                                   f'</article></body></html>') for s, t, d, x, pdf in ROWS}


class Split(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, module = load(SITE)
        captured = []
        def curl(url, retries=3):
            if url in DETAILS:
                return DETAILS[url]
            return PAGE if url == LIST1 else EMPTY
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', curl), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def pipeline(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, PAGE, LIST1))
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        return page, [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in page.items], parser

    def test_papers_equal_crawl(self):
        expected = self.native()
        self.assertEqual(1, len(expected))  # the Tribune is not listed, the short one is skipped
        page, papers, _ = self.pipeline()
        self.assertEqual(['premier-communique', 'communique-court'], [i.key for i in page.items])
        self.assertIsNone(papers[1])  # crawl()'s abstract < 50 skip -> rule skip
        self.assertEqual(clean_paper(expected[0]), papers[0])
        self.assertEqual('premier-communique', papers[0]['post_number'])
        self.assertEqual(PDF, papers[0]['pdf_url'])
        self.assertEqual('2026-05-28', papers[0]['published_date'])

    def test_empty_filtered_page_is_explicit_end_and_bad_detail_fails(self):
        _, _, parser = self.pipeline()
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, LIST1 + 'page/7/'))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        crawler, _ = load(SITE)
        with self.assertRaises(ValueError):
            crawler._enumeration_paper('<html><body>no article</body></html>',
                                       {'url': BASE + '/presse/x/'})


if __name__ == '__main__':
    unittest.main()
