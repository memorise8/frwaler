"""ND-1 (2026-10-19): sitemap route for health-ni-gov-uk-publications.
The live /publications listing ignores ?page= (every page returns the first 20 cards), so
the plan reads the site's sitemap. Identity and detail fields must equal crawl()'s paper;
the title comes from the detail <h1> (same text as the card on the live site) and the
card-only fields (listed_date, category, metadata.posted_date) are empty on this route.
Offline: synthetic fixture (detail <h1> set to the card title), no network."""
import copy
import json
import re
import unittest
from types import SimpleNamespace

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, load

SITE = 'health-ni-gov-uk-publications'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': 'https://www.health-ni.gov.uk',
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['html', 'url']},
            'sitemap': {'sitemap_url': 'https://www.health-ni.gov.uk/health/sitemap.xml',
                        'include': ['^https://www\\.health-ni\\.gov\\.uk/publications/(?!feed)[^/?#]+$']}}}}
CARD_ONLY = ('listed_date', 'category', 'metadata')


class SitemapRoute(SplitChecks, unittest.TestCase):
    SITE = SITE

    def with_h1(self):
        """Detail <h1> = the card title (as on the live site); the synthetic fixture's
        own <h1> differs from its card title, so it is replaced."""
        f = copy.deepcopy(self.fixture)
        crawler, _ = load(SITE)
        titles = {it['url']: it['title'] for it in crawler._parse_list_page(f['listing_body'])[0]}
        for url, html in f['details'].items():
            html = re.sub(r'<h1[^>]*>.*?</h1>', '', html, flags=re.S)
            f['details'][url] = html.replace('<main>', f'<main><h1>{titles[url]}</h1>', 1)
        return f

    def sitemap_papers(self, f):
        crawler, _ = load(SITE)
        return [crawler._enumeration_sitemap_paper(f['details'][u], u) for u in f['details']]

    def test_sitemap_paper_keeps_crawl_identity_and_detail_fields(self):
        f = self.with_h1()
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        actual = self.sitemap_papers(f)
        for e, a in zip(expected, actual):
            self.assertEqual({k: v for k, v in e.items() if k not in CARD_ONLY + ('published_date',)},
                             {k: v for k, v in a.items() if k not in CARD_ONLY + ('published_date',)})
            self.assertEqual(paper_identity(clean_paper(e)), paper_identity(clean_paper(a)))
            self.assertEqual(('', ''), (a['listed_date'], a['category']))
            meta_e, meta_a = json.loads(e['metadata']), json.loads(a['metadata'])
            meta_e.pop('posted_date'); self.assertEqual('', meta_a.pop('posted_date'))
            self.assertEqual(meta_e, meta_a)

    def test_missing_h1_is_not_a_paper(self):
        crawler, _ = load(SITE)
        url, html = next(iter(self.fixture['details'].items()))
        html = re.sub(r'<h1[^>]*>.*?</h1>', '', html, flags=re.S)
        self.assertIsNone(crawler._enumeration_sitemap_paper(html, url))

    def test_sitemap_plan_through_adapter(self):
        f = self.with_h1()
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        papers = []
        for url in f['details']:
            item = Item(key=url, url=url, date=None, payload={'detail_url': url})
            fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, f['details'][u], u)
            papers.append(adapter.materialize(item, SimpleNamespace(get=fetch)))
        self.assertEqual([clean_paper(x) for x in self.sitemap_papers(f)], papers)

    def test_include_pattern_scope(self):
        inc = re.compile(PLAN['sites'][SITE]['sitemap']['include'][0])
        self.assertTrue(inc.search('https://www.health-ni.gov.uk/publications/some-report'))
        self.assertFalse(inc.search('https://www.health-ni.gov.uk/publications/feed/health'))
        self.assertFalse(inc.search('https://www.health-ni.gov.uk/news/story'))


if __name__ == '__main__':
    unittest.main()
