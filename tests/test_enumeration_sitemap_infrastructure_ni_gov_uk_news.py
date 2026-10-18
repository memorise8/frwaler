"""ND-1 (2026-10-19): sitemap route for infrastructure-ni-gov-uk-news.
The live /news listing ignores ?page=, so the plan reads the sitemap. crawl() builds
every field from the detail page, so the sitemap paper must equal crawl()'s paper.
Offline: synthetic listing and detail pages, no network/process/database."""
import contextlib
import copy
import io
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'infrastructure-ni-gov-uk-news'
BASE = 'https://www.infrastructure-ni.gov.uk'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['html', 'url'], 'none_is_rule_skip': True},
            'sitemap': {'sitemap_url': BASE + '/infrastructure/sitemap.xml',
                        'include': ['^https://www\\.infrastructure-ni\\.gov\\.uk/news/(?!feed)[^/?#]+$']}}}}
LONG = 'Synthetic résumé of a roads announcement with Korean 자료 for offline equality testing.'
SLUGS = ['minister-opens-bridge', 'short-notice', 'no-title-page']


def detail(slug):
    title = '' if slug == 'no-title-page' else slug.replace('-', ' ').title()
    body = 'Too short here.' if slug == 'short-notice' else f'{LONG} {LONG}'
    return (f'<html><body><main><article class="node node--id-4242"><h1>{title}</h1>'
            f'<time datetime="2026-05-08T12:00:00Z">8 May 2026</time>'
            f'<div class="site-topics">Roads</div><p>{body}</p></article>'
            f'<nav><p>Navigation paragraph that is long enough to count.</p></nav></main></body></html>')


DETAILS = {f'{BASE}/news/{s}': detail(s) for s in SLUGS}
LISTING = '<html><body><main>' + ''.join(f'<a href="{u}">x</a>' for u in DETAILS) + '</main></body></html>'


class SitemapRoute(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, module = load(SITE)
        captured = []
        def transport(url, *a, **k):
            return DETAILS.get(url, LISTING)  # live listing ignores ?page=
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def test_sitemap_paper_equals_crawl_paper(self):
        expected = self.native()
        self.assertEqual(1, len(expected))  # short and untitled pages are skipped by crawl()
        crawler, _ = load(SITE)
        actual = [crawler._enumeration_sitemap_paper(h, u) for u, h in DETAILS.items()]
        self.assertEqual(expected, [a for a in actual if a is not None])
        self.assertEqual([None, None], actual[1:])
        self.assertEqual(paper_identity(clean_paper(expected[0])), paper_identity(clean_paper(actual[0])))

    def test_sitemap_plan_through_adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        url = f'{BASE}/news/{SLUGS[0]}'
        item = Item(key=url, url=url, date=None, payload={'detail_url': url})
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        self.assertEqual(clean_paper(crawler._enumeration_sitemap_paper(DETAILS[url], url)),
                         adapter.materialize(item, SimpleNamespace(get=fetch)))

    def test_include_pattern_scope(self):
        inc = re.compile(PLAN['sites'][SITE]['sitemap']['include'][0])
        self.assertTrue(inc.search(f'{BASE}/news/some-story'))
        self.assertFalse(inc.search(f'{BASE}/news/feed/infrastructure'))
        self.assertFalse(inc.search(f'{BASE}/publications/report'))


if __name__ == '__main__':
    unittest.main()
