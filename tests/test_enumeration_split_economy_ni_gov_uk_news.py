"""L2a (2026-10-18): sitemap route for economy-ni-gov-uk-news.
The live /news listing ignores ?page= (every page returns the first 20 items), so the
plan reads the site's sitemap. The sitemap paper must equal the paper crawl() saves.
Offline: synthetic fixture, no network/process/database."""
import json
import unittest
from types import SimpleNamespace

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split2_helpers import Split2Checks, load

SITE = 'economy-ni-gov-uk-news'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': 'https://www.economy-ni.gov.uk',
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['html', 'url']},
            'sitemap': {'sitemap_url': 'https://www.economy-ni.gov.uk/economy/sitemap.xml',
                        'include': ['^https://www\\.economy-ni\\.gov\\.uk/news/(?!feed)[^/?#]+$']}}}}


class SitemapRoute(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_sitemap_paper_equals_crawl_paper(self):
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        crawler, _ = load(SITE)
        urls = list(self.fixture['details'])
        actual = [crawler._enumeration_sitemap_paper(self.fixture['details'][u], u) for u in urls]
        self.assertEqual(expected, actual)

    def test_sitemap_plan_through_adapter(self):
        expected, _ = self.native()
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        papers = []
        for url in self.fixture['details']:
            item = Item(key=url, url=url, date=None, payload={'detail_url': url})
            fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, self.fixture['details'][u], u)
            papers.append(adapter.materialize(item, SimpleNamespace(get=fetch)))
        self.assertEqual([clean_paper(x) for x in expected], papers)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in papers])

    def test_include_pattern_scope(self):
        import re
        inc = re.compile(PLAN['sites'][SITE]['sitemap']['include'][0])
        self.assertTrue(inc.search('https://www.economy-ni.gov.uk/news/some-story'))
        self.assertFalse(inc.search('https://www.economy-ni.gov.uk/news/feed/economy'))
        self.assertFalse(inc.search('https://www.economy-ni.gov.uk/news/feed'))
        self.assertFalse(inc.search('https://www.economy-ni.gov.uk/publications/report'))


if __name__ == '__main__':
    unittest.main()
