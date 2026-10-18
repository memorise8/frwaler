"""ND-3 (2026-10-19): enumeration split for rki-de-de (GSB list, gtp page parameter).
_enumeration_items/_enumeration_paper must give crawl()'s papers (crawl() unchanged).
Offline: synthetic pages."""
import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'rki-de-de'
BASE = 'https://www.rki.de'
LIST1 = BASE + '/DE/Aktuelles/Neuigkeiten-und-Presse/Meldungen-PM/meldungen-pressemitteilungen-node.html'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1, 'none_is_rule_skip': True,
                        'list_method': '_enumeration_items', 'list_args': ['html'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['html', 'item']},
            'listing': {'url': LIST1 + '?gtp=16956152_Dokumente%253D{page}', 'first_page_url': LIST1,
                        'start_page': 1,
                        'total': {'type': 'regex', 'pattern': 'von\\s*<strong>(\\d+)</strong>'},
                        'parser': {'type': 'adapter', 'key_field': 'external_id', 'url_field': 'url',
                                   'date_field': 'listed_date', 'title_field': 'title',
                                   'listing_marker': '.l-search-wrapper__result-wrapper'}},
            'last_page': {'overlap_pages': 1}}}}
DIR = BASE + '/DE/Aktuelles/Neuigkeiten-und-Presse/Meldungen-PM/Meldungen/'
LONG = 'Das Robert Koch-Institut veröffentlicht eine synthetische Meldung für einen Offline-Test. '
ROWS = [('2026-10-06_Tool-Antibiotikatherapie', '06.10.2026', LONG * 3, 'Kurzer Teaser', True),
        ('2026-10-01_Kurz', '01.10.2026', 'Zu kurz.', 'Auch kurz.', False)]


def teaser(slug, date, teaser_text):
    return (f'<div class="c-teaser-search-result" role="listitem">'
            f'<a class="c-teaser-search-result__main-link" href="{DIR}{slug}.html?nn=16776990">'
            f'<h3 class="c-teaser-search-result__headline">{slug[11:].replace("-", " ")}</h3></a>'
            f'<p class="c-topline"><span class="c-topline__element is-type">Meldung</span>'
            f'<span class="c-topline__element is-date">{date}</span></p>'
            f'<p class="c-teaser-search-result__text">{teaser_text}</p></div>')


def listing(rows, total):
    return ('<html><body><p class="l-search-wrapper__result-count">Ergebnis <strong>1-25</strong> von '
            f'<strong>{total}</strong></p><section><div class="l-search-wrapper__result-wrapper" role="list">'
            + ''.join(teaser(s, d, t) for s, d, b, t, _ in rows) + '</div></section></body></html>')


PAGE = listing(ROWS, 2)
EMPTY = listing([], 2)
PDF = BASE + '/DE/Content/x/Bericht.pdf?__blob=publicationFile'
DETAILS = {f'{DIR}{s}.html': (f'<html><body><p class="l-article-wrapper__subline">Stand: {d}</p>'
                              f'<div class="l-article-wrapper__content"><p>{b}</p>'
                              f'{f"<a href={PDF}>pdf</a>" if pdf else ""}<nav>Navigation</nav></div></body></html>')
           for s, d, b, t, pdf in ROWS}


class Split(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, module = load(SITE)
        captured = []
        def get(url, retries=3):
            return DETAILS.get(url) or (PAGE if url == LIST1 else EMPTY)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', get), patch('time.sleep', lambda *a: None), \
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
        self.assertEqual(1, len(expected))
        page, papers, _ = self.pipeline()
        self.assertEqual(['2026-10-06_Tool-Antibiotikatherapie', '2026-10-01_Kurz'], [i.key for i in page.items])
        self.assertIsNone(papers[1])  # crawl()'s abstract < 50 skip -> rule skip
        self.assertEqual(clean_paper(expected[0]), papers[0])
        self.assertEqual('20261006', papers[0]['post_number'])
        self.assertEqual(PDF, papers[0]['pdf_url'])

    def test_empty_page_is_explicit_end_and_empty_detail_fails(self):
        _, _, parser = self.pipeline()
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, LIST1))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        crawler, _ = load(SITE)
        with self.assertRaises(ValueError):
            crawler._enumeration_paper('', {'url': DIR + 'x.html'})


if __name__ == '__main__':
    unittest.main()
