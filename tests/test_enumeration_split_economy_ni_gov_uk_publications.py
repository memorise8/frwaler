"""L2a (2026-10-18): sitemap route for economy-ni-gov-uk-publications.
crawl() reads the RSS feed plus two hard-coded sitemap pages and stops at 200x10 items;
the plan reads the whole sitemap index instead. The sitemap paper must equal the paper
crawl() saves for a sitemap-phase item. Offline: synthetic HTML, no network/process/DB."""
import contextlib
import copy
import io
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'economy-ni-gov-uk-publications'
BASE = 'https://www.economy-ni.gov.uk'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['raw', 'url']},
            'sitemap': {'sitemap_url': BASE + '/economy/sitemap.xml',
                        'include': ['^https://www\\.economy-ni\\.gov\\.uk/publications/[^?#]+$'],
                        'exclude': ['/publications/feed(/|$)']}}}}
LONG = ('Synthetic statistical bulletin résumé with Korean 자료 for offline equality testing. ' * 4).strip()


def detail(n, nid):
    return (f'<html><head><link rel="shortlink" href="{BASE}/node/{nid}"></head><body>'
            f'<h1 class="page-title">Report {n}</h1><time datetime="2024-0{n}-1{n}T09:00:00Z">x</time>'
            f'<div class="page-summary"><p>{LONG}</p></div>'
            f'<div class="article-content"><h2>Documents</h2><a href="/sites/default/files/r{n}.pdf">PDF</a></div>'
            f'<ul class="site-topics--list"><li>Statistics,</li><li>Energy</li></ul></body></html>').encode()


URLS = [BASE + '/publications/report-one', BASE + '/publications/report-two']
DETAILS = {URLS[0]: detail(1, 1001), URLS[1]: detail(2, 1002)}
SITEMAP = ('<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           + ''.join(f'<url><loc>{u}</loc></url>' for u in URLS + [BASE + '/news/x']) + '</urlset>').encode()


class SitemapRoute(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, details=DETAILS):
        crawler, _ = load(SITE)
        captured = []

        def transport(url, retries=3):
            if url == crawler._RSS_URL:
                return b'<rss><channel></channel></rss>'
            if url in crawler._SITEMAP_PAGES:
                return SITEMAP if url.endswith('page=2') else b'<urlset/>'
            return details[url]
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(2, crawler.crawl(limit=None))
        return captured

    def test_sitemap_paper_equals_crawl_paper(self):
        expected = self.native()
        crawler, _ = load(SITE)
        with contextlib.redirect_stdout(io.StringIO()):
            actual = [crawler._enumeration_sitemap_paper(DETAILS[u], u) for u in URLS]
        self.assertEqual(expected, actual)
        self.assertEqual(['1001', '1002'], [p['post_number'] for p in actual])
        self.assertNotIn('_curl', crawler.__dict__, 'temporary transport must be removed')

    def test_short_abstract_skip_is_preserved(self):
        short = {u: v.replace(LONG.encode(), b'Short.') for u, v in DETAILS.items()}
        crawler, _ = load(SITE)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual([None, None], [crawler._enumeration_sitemap_paper(short[u], u) for u in URLS])

    def test_sitemap_plan_through_adapter(self):
        expected = self.native()
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        with contextlib.redirect_stdout(io.StringIO()):
            papers = [adapter.materialize(Item(key=u, url=u, date=None, payload={'detail_url': u}),
                                          SimpleNamespace(get=fetch)) for u in URLS]
        self.assertEqual([clean_paper(x) for x in expected], papers)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in papers])

    def test_include_pattern_scope(self):
        sm = PLAN['sites'][SITE]['sitemap']
        inc, exc = re.compile(sm['include'][0]), re.compile(sm['exclude'][0])
        wanted = lambda u: bool(inc.search(u)) and not exc.search(u)
        self.assertTrue(wanted(URLS[0]))
        self.assertFalse(wanted(BASE + '/publications/feed/economy'))
        self.assertFalse(wanted(BASE + '/news/x'))


if __name__ == '__main__':
    unittest.main()
