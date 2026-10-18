"""ND-1 (2026-10-19): sitemap route for education-ni-gov-uk-news.
The live /news listing ignores ?page= (every page returns the first 20 items), so the
plan reads the site's sitemap. Identity and detail fields must equal crawl()'s paper;
listing-only fields (listed_date, snippet, list_page) are absent on this route.
Offline: synthetic fixture, no network/process/database."""
import json
import re
import unittest
from types import SimpleNamespace

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import Item
from crawler.enumeration.plan import site_plan
from tests.collector_split2_helpers import Split2Checks, load

SITE = 'education-ni-gov-uk-news'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'sitemap', 'base_url': 'https://www.education-ni.gov.uk',
            'adapter': {'type': 'crawler', 'detail_method': '_enumeration_sitemap_paper',
                        'detail_args': ['html', 'url']},
            'sitemap': {'sitemap_url': 'https://www.education-ni.gov.uk/education/sitemap.xml',
                        'include': ['^https://www\\.education-ni\\.gov\\.uk/news/(?!feed)[^/?#]+$']}}}}
DETAIL_FIELDS = ('site_id', 'external_id', 'post_number', 'title', 'abstract', 'authors', 'publisher',
                 'department', 'journal', 'category', 'keywords', 'url', 'pdf_url', 'doi', 'original_filename')


class SitemapRoute(Split2Checks, unittest.TestCase):
    SITE = SITE

    def sitemap_papers(self):
        crawler, _ = load(SITE)
        return [crawler._enumeration_sitemap_paper(self.fixture['details'][u], u) for u in self.fixture['details']]

    def test_sitemap_paper_keeps_crawl_identity_and_detail_fields(self):
        expected, _ = self.native()
        self.assertEqual(2, len(expected))
        actual = self.sitemap_papers()
        for e, a in zip(expected, actual):
            self.assertEqual({k: e[k] for k in DETAIL_FIELDS}, {k: a[k] for k in DETAIL_FIELDS})
            self.assertEqual(paper_identity(clean_paper(e)), paper_identity(clean_paper(a)))
            self.assertEqual('', a['listed_date'])
            meta = json.loads(a['metadata'])
            self.assertIsNone(meta['list_page'])
            self.assertEqual('', meta['snippet'])

    def test_sitemap_plan_through_adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        papers = []
        for url in self.fixture['details']:
            item = Item(key=url, url=url, date=None, payload={'detail_url': url})
            fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, self.fixture['details'][u], u)
            papers.append(adapter.materialize(item, SimpleNamespace(get=fetch)))
        self.assertEqual([clean_paper(x) for x in self.sitemap_papers()], papers)

    def test_include_pattern_scope(self):
        inc = re.compile(PLAN['sites'][SITE]['sitemap']['include'][0])
        self.assertTrue(inc.search('https://www.education-ni.gov.uk/news/some-story'))
        self.assertFalse(inc.search('https://www.education-ni.gov.uk/news/feed/education'))
        self.assertFalse(inc.search('https://www.education-ni.gov.uk/publications/report'))


if __name__ == '__main__':
    unittest.main()
