"""ND-1 (2026-10-19): sitemap route for health-ni-gov-uk-news.
The RSS feed ignores ?page= on the live site, so the plan reads the sitemap. The sitemap
paper must equal crawl()'s paper except metadata.rss_description (no RSS item on this
route); the RSS title/date/creator are the detail page's <h1>, 'Date published' <time>
text and the default publisher. Offline: synthetic RSS and detail pages, no network."""
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

SITE = 'health-ni-gov-uk-news'
BASE = 'https://www.health-ni.gov.uk'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['html', 'url'], 'none_is_rule_skip': True},
            'sitemap': {'sitemap_url': BASE + '/health/sitemap.xml',
                        'include': ['^https://www\\.health-ni\\.gov\\.uk/news/(?!feed)[^/?#]+$']}}}}
LONG = 'Synthetic résumé of a departmental announcement with Korean 자료 for offline testing. ' * 3
NEWS = [('minister-visits-clinic', 'Minister visits clinic', '16 October 2026', '425389', LONG),
        ('statistics-published', 'Statistics published', '3 October 2025', None, LONG),
        ('short-item', 'Short item', '1 May 2024', '425001', 'Too short.')]


def detail(slug, title, date, node, body):
    node_link = f'<link rel="shortlink" href="{BASE}/node/{node}" />' if node else ''
    return (f'<html><head>{node_link}</head><body><main id="main-content"><article>'
            f'<h1 class="page-title">{title}</h1><p class="published-date"><span>Date published: </span>'
            f'<span><time datetime="2026-01-01T12:00:00Z">{date}</time></span></p>'
            f'<div class="page-summary"><p>{body}</p></div></article></main></body></html>')


RSS = ('<?xml version="1.0"?><rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
       + ''.join(f'<item><title>{t}</title><link>{BASE}/news/{s}</link>'
                 f'<description>{LONG if s == "short-item" else "Listing teaser."}</description>'
                 f'<pubDate>{d}</pubDate><dc:creator>Department of Health</dc:creator></item>'
                 for s, t, d, n, b in NEWS)
       + '</channel></rss>')
DETAILS = {f'{BASE}/news/{s}': detail(s, t, d, n, b) for s, t, d, n, b in NEWS}


class SitemapRoute(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, module = load(SITE)
        captured, rss_calls = [], []
        def transport(url, *a, **k):
            if url in DETAILS:
                return DETAILS[url]
            rss_calls.append(url)
            return RSS if len(rss_calls) == 1 else RSS  # live feed ignores ?page=
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_get', transport), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def test_detail_text_equals_fetch_detail(self):
        crawler, module = load(SITE)
        for url, html in DETAILS.items():
            with patch.object(module, '_curl_get', lambda u, **k: html):
                self.assertEqual(module._fetch_detail(url), crawler._enumeration_detail_text(html))

    def test_sitemap_paper_equals_crawl_paper_except_rss_description(self):
        expected = self.native()
        self.assertEqual(3, len(expected))  # crawl() keeps the short item via the RSS description
        crawler, _ = load(SITE)
        actual = {u: crawler._enumeration_sitemap_paper(h, u) for u, h in DETAILS.items()}
        self.assertIsNone(actual[f'{BASE}/news/short-item'])  # no RSS description on this route
        for e in expected[:2]:
            a = actual[e['url']]
            self.assertEqual({k: v for k, v in e.items() if k != 'metadata'},
                             {k: v for k, v in a.items() if k != 'metadata'})
            me, ma = json.loads(e['metadata']), json.loads(a['metadata'])
            self.assertEqual('', ma.pop('rss_description')); me.pop('rss_description')
            self.assertEqual(me, ma)
            self.assertEqual(paper_identity(clean_paper(e)), paper_identity(clean_paper(a)))
        self.assertEqual('2026-10-16', actual[f'{BASE}/news/minister-visits-clinic']['published_date'])
        self.assertEqual('statistics-published', actual[f'{BASE}/news/statistics-published']['external_id'])

    def test_sitemap_plan_through_adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        url = f'{BASE}/news/minister-visits-clinic'
        item = Item(key=url, url=url, date=None, payload={'detail_url': url})
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        self.assertEqual(clean_paper(crawler._enumeration_sitemap_paper(DETAILS[url], url)),
                         adapter.materialize(item, SimpleNamespace(get=fetch)))

    def test_include_pattern_scope(self):
        inc = re.compile(PLAN['sites'][SITE]['sitemap']['include'][0])
        self.assertTrue(inc.search(f'{BASE}/news/some-story'))
        self.assertFalse(inc.search(f'{BASE}/news/feed/health'))
        self.assertFalse(inc.search(f'{BASE}/publications/report'))


if __name__ == '__main__':
    unittest.main()
