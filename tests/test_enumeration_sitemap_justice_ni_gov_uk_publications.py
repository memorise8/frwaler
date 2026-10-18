"""ND-1 (2026-10-19): sitemap route for justice-ni-gov-uk-publications.
The live /publications listing ignores ?page=, so the plan reads the sitemap. Identity
and detail fields must equal crawl()'s paper; the title is the detail <h1> (the card
title on the live site) and card-only fields (listed_date, category, card summary) are
empty, so the abstract is the meta description. Offline: synthetic pages, no network."""
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

SITE = 'justice-ni-gov-uk-publications'
BASE = 'https://www.justice-ni.gov.uk'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['html', 'url'], 'none_is_rule_skip': True},
            'sitemap': {'sitemap_url': BASE + '/justice/sitemap.xml',
                        'include': ['^https://www\\.justice-ni\\.gov\\.uk/publications/(?!feed)[^/?#]+$']}}}}
META = 'Synthetic summary of a justice statistics bulletin with Korean 자료 for offline equality tests, long enough.'
PUBS = [('prison-statistics-2025', 'Prison statistics 2025', '777', True),
        ('court-bulletin', 'Court bulletin', None, False),
        ('tiny-note', 'Tiny note', '778', None)]


def detail(slug, title, node, pdf):
    meta = 'Short.' if pdf is None else META
    link = f'<link rel="shortlink" href="{BASE}/node/{node}" />' if node else ''
    pdf_a = f'<a href="/sites/default/files/publications/{slug}%20report.pdf">PDF</a>' if pdf else ''
    return (f'<html><head>{link}<meta name="description" content="{meta}"></head><body><main>'
            f'<h1 class="page-title">{title}</h1><time datetime="2026-03-02T09:00:00Z">2 March 2026</time>'
            f'<div class="field--name-field-site-topics"><a href="/t">Prisons</a></div>'
            f'<div class="article-content">{pdf_a}</div></main></body></html>')


DETAILS = {f'{BASE}/publications/{s}': detail(s, t, n, p) for s, t, n, p in PUBS}
LISTING = ('<html><body><ul class="card-deck--search-results">'
           + ''.join(f'<li><a class="card" href="/publications/{s}"><h3 class="card__title">{t}</h3></a>'
                     f'<time datetime="2026-03-01T00:00:00Z"></time><p class="card__content">Card.</p>'
                     f'<span class="field-publication-type">Statistics</span></li>' for s, t, n, p in PUBS)
           + '</ul></body></html>')
CARD_ONLY = ('listed_date', 'category', 'metadata')


class SitemapRoute(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured = []
        crawler._curl_get = lambda url: DETAILS.get(url, LISTING)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def test_sitemap_paper_keeps_crawl_identity_and_detail_fields(self):
        expected = self.native()
        self.assertEqual(2, len(expected))  # crawl() skips the short-abstract item
        crawler, _ = load(SITE)
        actual = {u: crawler._enumeration_sitemap_paper(h, u) for u, h in DETAILS.items()}
        self.assertIsNone(actual[f'{BASE}/publications/tiny-note'])
        for e in expected:
            a = actual[e['url']]
            self.assertEqual({k: v for k, v in e.items() if k not in CARD_ONLY},
                             {k: v for k, v in a.items() if k not in CARD_ONLY})
            self.assertEqual(paper_identity(clean_paper(e)), paper_identity(clean_paper(a)))
            self.assertEqual(('', ''), (a['listed_date'], a['category']))
            self.assertEqual({'posted_date': '', 'node_id': json.loads(e['metadata'])['node_id'],
                              'card_summary_raw': '', 'publication_type': ''}, json.loads(a['metadata']))
        self.assertEqual('court-bulletin', actual[f'{BASE}/publications/court-bulletin']['post_number'])

    def test_sitemap_plan_through_adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        url = f'{BASE}/publications/{PUBS[0][0]}'
        item = Item(key=url, url=url, date=None, payload={'detail_url': url})
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, DETAILS[u], u)
        self.assertEqual(clean_paper(crawler._enumeration_sitemap_paper(DETAILS[url], url)),
                         adapter.materialize(item, SimpleNamespace(get=fetch)))

    def test_include_pattern_scope(self):
        inc = re.compile(PLAN['sites'][SITE]['sitemap']['include'][0])
        self.assertTrue(inc.search(f'{BASE}/publications/some-report'))
        self.assertFalse(inc.search(f'{BASE}/publications/feed/justice'))
        self.assertFalse(inc.search(f'{BASE}/news/story'))


if __name__ == '__main__':
    unittest.main()
