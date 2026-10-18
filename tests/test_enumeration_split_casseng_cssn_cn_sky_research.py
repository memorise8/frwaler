"""casseng-cssn-cn-sky_research: path-paged listing (ext:path_paging) + split methods.

Memory only: the adapter output must equal the Paper the unchanged crawl() saves.
"""
import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'casseng-cssn-cn-sky_research'
BASE = 'http://casseng.cssn.cn/sky_research/publications/journals/'
PLAN = {'version': 1, 'sites': {SITE: {
    'strategy': 'last_page_backward', 'base_url': 'http://casseng.cssn.cn',
    'adapter': {'type': 'crawler', 'contract_version': 1,
                'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                'detail_method': '_enumeration_detail', 'detail_args': ['html', 'item', 'url']},
    'listing': {'url': BASE + 'index_{page}.shtml', 'first_page_url': BASE, 'start_page': 0, 'page_size': 8,
                'parser': {'type': 'adapter', 'key_field': 'url', 'url_field': 'url', 'title_field': 'title'}},
    'last_page': {'overlap_pages': 1}}}}
LONG = 'Sponsor: Synthetic Institute of Testing. ' * 3


def listing(page, count, ids):
    links = ''.join(f'<li><a href="./2023/t20230714_{i}.shtml" title="Journal {i}">Journal {i}</a></li>' for i in ids)
    return (f'<html><head><script>var countPage = {count};var currentPage = {page};</script></head>'
            f'<body><ul class="cont3-1text cont3-2text">{links}</ul></body></html>')


def detail(i, text=LONG):
    return (f'<html><head><meta name="contentid" content="{i}"><meta name="publishdate" content="2021-12-0{i % 9 + 1}">'
            f'</head><body><h2 class="qt_article_title">Journal {i}</h2>'
            f'<div class="about-content news-detail"><strong>ISSN:</strong> 1000-{i}<br>{text}</div></body></html>')


def site_fixture(text=LONG):
    pages = {BASE: listing(0, 2, [11, 12]), BASE + 'index_1.shtml': listing(1, 2, [13])}
    details = {BASE + f'2023/t20230714_{i}.shtml': detail(i, text) for i in (11, 12, 13)}
    return pages, details


class CassengSplit(unittest.TestCase):
    def native(self, pages, details):
        crawler, _ = load(SITE)
        saved = []
        crawler.detail_delay = 0
        crawler._check_cancelled = lambda: None
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        crawler._curl_get = lambda url, referer=None, timeout=45: pages.get(url) or details.get(url)
        with contextlib.redirect_stdout(io.StringIO()), patch('time.sleep', lambda *a: None):
            crawler.crawl()
        return saved

    def pipeline(self, pages, details):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=crawler)
        parser = ListingParser(plan.listing['parser'], adapter=adapter)
        papers = []
        fetch = lambda url: Response(200, {'Content-Type': 'text/html'}, details[url], url)
        for url, body in pages.items():
            for item in parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url)).items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return papers

    def test_papers_and_identity_equal_original_crawl(self):
        pages, details = site_fixture()
        expected = self.native(pages, details)
        self.assertEqual(3, len(expected))
        actual = self.pipeline(pages, details)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(['11', '12', '13'], [paper_identity(p)[0] for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        pages, details = site_fixture(text='')
        details = {u: b.replace('<strong>ISSN:</strong> 1000-', '') for u, b in details.items()}
        self.assertEqual([], self.native(pages, details))
        self.assertEqual([], self.pipeline(pages, details))

    def test_path_page_urls(self):
        from crawler.enumeration.strategies import Strategy
        plan = site_plan(PLAN, SITE, registered=[SITE])
        strat = Strategy(SimpleNamespace(plan=plan, fetcher=None, parser=None, store=None, site_id=SITE, today=None))
        self.assertEqual(BASE, str(strat.page_url(0)))
        self.assertEqual(BASE + 'index_11.shtml', str(strat.page_url(11)))

    def test_wrong_or_missing_pagination_is_a_parse_error(self):
        crawler, _ = load(SITE)
        for body, url in ((listing(0, 2, [11]), BASE + 'index_1.shtml'),   # page served for another number
                          (listing(1, 1, [11]), BASE + 'index_1.shtml'),   # page beyond countPage
                          ('<html><body>maintenance</body></html>', BASE),
                          (listing(0, 2, []), BASE)):                      # declared page without entries
            with self.assertRaises(PermanentFailure) as caught, guarded():
                crawler._enumeration_items(body, url)
            self.assertTrue(caught.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
