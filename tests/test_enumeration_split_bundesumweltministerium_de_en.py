"""ND-3 (2026-10-19): enumeration split for bundesumweltministerium-de-en (TYPO3 publications,
English filter). _enumeration_items/_enumeration_paper must give crawl()'s papers (crawl()
unchanged), including the effective (redirected) detail URL. Offline: synthetic pages."""
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

SITE = 'bundesumweltministerium-de-en'
BASE = 'https://www.bundesumweltministerium.de'
START = BASE + '/en/public-information-service/publications'
LIST1 = START + '?tx_bmubpublications_publications%5Bfilter%5D%5BlanguageEn%5D=2'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1, 'none_is_rule_skip': True,
                        'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'final_url', 'item']},
            'listing': {'url': START + '?tx_bmubpublications_publications%5BcurrentPage%5D={page}',
                        'first_page_url': START,
                        'params': {'tx_bmubpublications_publications[filter][languageEn]': '2'},
                        'start_page': 1,
                        'parser': {'type': 'adapter', 'key_field': '_key', 'url_field': 'url',
                                   'title_field': 'title', 'listing_marker': '.c-publications-list'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'The ministry publishes a synthetic English brochure for an offline equality test of this split. '
ROWS = [('758', 'Waste Management in Germany 2025', 'English', LONG * 2),
        ('726', 'Kurzbroschüre', 'German', LONG * 2),
        ('724', 'Short Note', 'English', 'Too short to keep, under one hundred characters.')]


def article(pid, title, lang):
    return (f'<article class="c-publications-list__item" id="publication-{pid}">'
            f'<h3 class="c-publications-list__title"><a href="/PU{pid}-1">{title}</a></h3>'
            f'<div class="c-publications-list__content"><small>Brochure | No. {pid}A | {lang}</small></div>'
            f'<a download href="/fileadmin/{pid}.pdf">PDF</a></article>')


def listing(rows):
    return ('<html><body><div class="c-articles-list c-publications-list" id="cListAnchor">'
            + ''.join(article(p, t, l) for p, t, l, _ in rows) + '</div></body></html>')


PAGE = listing(ROWS)
EMPTY = listing([])
LONGURL = {p: f'{BASE}/en/publication/{p}-long-title' for p, *_ in ROWS}
DETAILS = {LONGURL[p]: (f'<html><head><meta name="description" content="x"></head><body><article id="article">'
                        f'<h1 itemprop="headline">{t}</h1><meta itemprop="datePublished" content="2025-11-03">'
                        f'<div class="c-text"><h2>About</h2><div class="c-ce-formated"><p>{a}</p></div></div>'
                        f'<div class="c-meta-facts__row"><span class="c-meta-facts__label">Language:</span> {l}</div>'
                        f'</article></body></html>') for p, t, l, a in ROWS}


class Split(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured = []
        def get(url, context='request', referer=None, accept=None):
            if url.startswith(BASE + '/PU'):
                pid = url[len(BASE) + 3:].split('-')[0]
                return DETAILS[LONGURL[pid]], LONGURL[pid]
            if url == LIST1:
                return PAGE, url
            return EMPTY, url  # the start page (filter discovery falls back to the constant)
        crawler._curl_get_text = get
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def pipeline(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, PAGE, LIST1))
        def fetch(u):
            pid = u[len(BASE) + 3:].split('-')[0]
            return Response(200, {'Content-Type': 'text/html'}, DETAILS[LONGURL[pid]], LONGURL[pid])
        return page, [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in page.items], parser

    def test_papers_equal_crawl(self):
        expected = self.native()
        self.assertEqual(1, len(expected))
        page, papers, _ = self.pipeline()
        self.assertEqual(['758', '726', '724'], [i.key for i in page.items])
        self.assertEqual([None, None], papers[1:])  # German record, abstract < 100: crawl()'s skips
        self.assertEqual(clean_paper(expected[0]), papers[0])
        self.assertEqual(LONGURL['758'], papers[0]['url'])
        self.assertNotIn('_key', json.loads(papers[0]['metadata'])['list_record'])

    def test_empty_page_is_explicit_end(self):
        _, _, parser = self.pipeline()
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, LIST1))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))


if __name__ == '__main__':
    unittest.main()
