"""lib-cssn-cn-zy: path-paged listing (ext:path_paging) + split methods.

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
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'lib-cssn-cn-zy'
LIST = 'http://lib.cssn.cn/zy/dzzy/sjk_1/axkflll/'
DETAIL = 'http://lib.cssn.cn/zy/dzzy/sjk_1/'
PLAN = {'version': 1, 'sites': {SITE: {
    'strategy': 'last_page_backward', 'base_url': 'http://lib.cssn.cn',
    'adapter': {'type': 'crawler', 'contract_version': 1,
                'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                'detail_method': '_enumeration_detail', 'detail_args': ['html', 'url']},
    'listing': {'url': LIST + 'index_{page}.shtml', 'first_page_url': LIST, 'start_page': 0, 'page_size': 25,
                'parser': {'type': 'adapter', 'key_field': 'url', 'url_field': 'url', 'title_field': 'title'}},
    'last_page': {'overlap_pages': 1}}}}
LONG = '综合测试数据库介绍。' * 15


def listing(hrefs):
    spans = ''.join(f'<li><span class="detail"><a href="{h}">资源</a></span></li>' for h in hrefs)
    return f'<html><body><ul>{spans}</ul></body></html>'


def detail(i, text=LONG):
    return (f'<html><body><h1>数据库 {i}</h1><table>'
            f'<tr><td>数据库介绍</td><td>{text}</td></tr><tr><td>学科</td><td>历史</td></tr>'
            f'<tr><td>数据库网址</td><td><a href="http://db.example.test/{i}">link</a></td></tr>'
            f'</table></body></html>')


def fixture(text=LONG):
    hrefs = ['../201805/t20180517_4296241.shtml', '../202509/t20250922_5916628.shtml']
    pages = {LIST: listing(hrefs)}
    details = {DETAIL + h[3:]: detail(n, text) for n, h in enumerate(hrefs)}
    return pages, details


class LibCssnSplit(unittest.TestCase):
    def native(self, pages, details):
        crawler, module = load(SITE)
        saved = []
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        get = lambda url, retries=3: pages.get(url) or details.get(url) or '<html><body></body></html>'
        with contextlib.redirect_stdout(io.StringIO()), patch.object(module, '_curl_get', get), \
                patch('time.sleep', lambda *a: None):
            crawler.crawl()
        return saved

    def pipeline(self, pages, details):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=crawler)
        parser = ListingParser(plan.listing['parser'], adapter=adapter)
        fetch = lambda url: Response(200, {'Content-Type': 'text/html'}, details[url], url)
        papers = []
        for url, body in pages.items():
            for item in parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url)).items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return papers

    def test_papers_and_identity_equal_original_crawl(self):
        pages, details = fixture()
        expected = self.native(pages, details)
        self.assertEqual(2, len(expected))
        actual = self.pipeline(pages, details)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(['t20180517_4296241', 't20250922_5916628'], [paper_identity(p)[0] for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        pages, details = fixture(text='短')
        self.assertEqual([], self.native(pages, details))
        self.assertEqual([], self.pipeline(pages, details))

    def test_path_page_urls(self):
        from crawler.enumeration.strategies import Strategy
        plan = site_plan(PLAN, SITE, registered=[SITE])
        strat = Strategy(SimpleNamespace(plan=plan, fetcher=None, parser=None, store=None, site_id=SITE, today=None))
        self.assertEqual(LIST, str(strat.page_url(0)))
        self.assertEqual(LIST + 'index_1.shtml', str(strat.page_url(1)))

    def test_empty_200_page_is_not_an_end(self):
        plan = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(plan.listing['parser'], adapter=Adapter(SITE, plan.section('adapter'), None, crawler=crawler))
        with guarded():
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', LIST))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('parse_error'))
        self.assertFalse(page.extra.get('explicit_empty', False))


if __name__ == '__main__':
    unittest.main()
