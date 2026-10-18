"""ND-1 (2026-10-19): fi-se-en through the list page's own "load more" JSON.
?year= pages show only the newest 10 items up to that year, so the plan pages through
/en/published/reports/reports/loadmore?page=N. Papers must equal crawl()'s except
metadata.year_filter (crawl(): year page where first seen; here: the item's year).
Offline: synthetic HTML/JSON built from the same records, no network."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'fi-se-en'
BASE = 'https://www.fi.se'
LIST = BASE + '/en/published/reports/reports/'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'contract_version': 1,
                        'list_method': '_enumeration_items', 'list_args': ['json', 'url'],
                        'detail_method': '_enumeration_detail', 'detail_args': ['html'],
                        'paper_method': '_enumeration_paper', 'paper_args': ['item', 'detail'],
                        'none_is_rule_skip': True},
            'listing': {'url': LIST + 'loadmore', 'page_param': 'page', 'start_page': 1, 'page_size': 10,
                        'parser': {'type': 'adapter', 'url_field': 'url', 'key_field': 'external_id',
                                   'date_field': 'listed_date', 'title_field': 'title'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = 'Synthetic Finansinspektionen report résumé with Korean 자료 for offline equality testing. ' * 2
RECS = [(101931, 2026, 'first-report', 'Report: First', '2026-06-18', ['Payments', 'Reports'], LONG),
        (100731, 2026, 'second-report', 'Second report', '2026-04-29', ['Sustainability'], 'Short.'),
        (99107, 2025, 'third-report', 'Third report', '2025-11-19', [], '')]


def href(year, slug):
    return f'/en/published/reports/reports/{year}/{slug}/?language=en'


HTML = ('<html><body><a href="?year=2026">2026</a><div id="results">' + ''.join(
    f'<div class="list-item"><h2><a href="{href(y, s)}">{t}</a></h2><span class="date">{d}</span>'
    f'<p class="introduction">Intro of {s} that is long enough to pad a short body text here.</p>'
    + ''.join(f'<a class="categoryLink" href="/c">{c}</a>' for c in cats) + '</div>'
    for cid, y, s, t, d, cats, body in RECS) + '</div></body></html>')
JSON = {'typ': 'Structure', 'totalHits': 3, 'items': [
    {'typ': 'item', 'title': t, 'published': d, 'url': href(y, s), 'year': y, 'contentId': cid,
     'ingress': f'Intro of {s} that is long enough to pad a short body text here. ',
     'topCategoryLinks': {str(c2): [{'Url': '/c', 'Text': x} for x in cats2] for c2, _, _, _, _, cats2, _ in RECS}}
    for cid, y, s, t, d, cats, body in RECS]}
DETAILS = {BASE + href(y, s): (f'<html><body><div class="editor-content"><p>{body}</p></div>'
                               f'<a href="/contentassets/{s}.pdf">PDF</a></body></html>' if body else
                               '<html><body><p>tiny</p></body></html>')
           for cid, y, s, t, d, cats, body in RECS}


class Split(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured = []
        def get(url, max_time=30):
            if url in DETAILS:
                return DETAILS[url]
            return HTML if url in (LIST, LIST + '?year=2026') else '<html><body><div id="results"></div></body></html>'
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.dict(crawler.crawl.__globals__, {'_curl_get': get}), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def pipeline(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, json.dumps(JSON), LIST + 'loadmore?page=1'))
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        return [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in page.items]

    def test_papers_equal_crawl_except_year_filter(self):
        expected = self.native()
        self.assertEqual(3, len(expected))  # short/empty bodies are padded with the intro
        actual = [p for p in self.pipeline() if p is not None]
        self.assertEqual(len(expected), len(actual))
        for e, a in zip(expected, actual):
            e = clean_paper(e)
            me, ma = json.loads(e.pop('metadata')), json.loads(a.pop('metadata'))
            self.assertEqual(2026, me.pop('year_filter'))
            ma.pop('year_filter')
            self.assertEqual(me, ma)
            self.assertEqual(e, a)
        self.assertEqual('2026/first-report', actual[0]['post_number'])

    def test_short_abstract_is_skipped(self):
        crawler, _ = load(SITE)
        item = dict(crawler._enumeration_items(JSON)[2], intro='Too short.')
        self.assertIsNone(crawler._enumeration_paper(item, crawler._enumeration_detail(DETAILS[item['url']])))

    def test_bad_shape_and_empty_page(self):
        crawler, _ = load(SITE)
        self.assertEqual([], crawler._enumeration_items({'typ': 'Structure', 'totalHits': 3, 'items': []}))
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'html': ''})


if __name__ == '__main__':
    unittest.main()
