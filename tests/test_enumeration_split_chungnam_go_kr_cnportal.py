"""L2a (2026-10-18): enumeration methods for chungnam-go-kr-cnportal.
The enumeration path must produce the paper the unchanged crawl() saves
(external_id = nttId, url). Offline: synthetic HTML, no network/process/DB."""
import contextlib
import copy
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'chungnam-go-kr-cnportal'
BASE = 'https://www.chungnam.go.kr'
LIST = BASE + '/cnportal/cnapcPressList/cnapcPress/list.do'
PLAN = {'version': 1, 'defaults': {'request_interval_s': 1.5, 'respect_robots': True, 'retries': 1},
        'sites': {SITE: {
            'strategy': 'last_page_backward', 'base_url': BASE,
            'adapter': {'type': 'crawler', 'list_method': '_enumeration_items', 'list_args': ['html', 'url'],
                        'detail_method': '_enumeration_paper', 'detail_args': ['raw', 'item']},
            'listing': {'url': LIST, 'params': {'menuNo': '500498', 'pageIndex': 1, 'pageUnit': '30'},
                        'page_param': 'pageIndex', 'start_page': 1,
                        'parser': {'type': 'adapter', 'key_field': 'external_id', 'url_field': 'url',
                                   'title_field': 'title', 'date_field': 'listed_date',
                                   'listing_marker': '.txt-board'}},
            'last_page': {'overlap_pages': 1}}}}
LONG = '충청남도 합성 보도자료 본문입니다. 오프라인 동등성 시험용 résumé 텍스트. ' * 4


def row(no, ntt, title):
    return (f'<tr><td>{no}</td><td class="td-subj"><a class="tit" href="/cnportal/cnapcPressList/cnapcPress/'
            f'view.do?nttId={ntt}&amp;menuNo=500498&amp;pageIndex=1">{title}</a></td><td>-</td>'
            f'<td>기획조정실</td><td>2026-10-1{no}</td><td>12</td></tr>')


LISTING = ('<html><body><table class="txt-board"><tbody>' + row(2, 9002, '보도 둘') + row(1, 9001, '보도 하나')
           + '</tbody></table></body></html>')
EMPTY = '<html><body><table class="txt-board"><tbody></tbody></table></body></html>'
URLS = [BASE + f'/cnportal/cnapcPressList/cnapcPress/view.do?nttId={n}&menuNo=500498&pageIndex=1'
        for n in (9002, 9001)]


def detail(n, with_name):
    name = f'보도{n}.pdf' if with_name else ''
    return (f'<html><body><div class="board-view-title">보도 {n}</div><ul class="board-view-li">'
            f'<li class="board-view-inner"><span class="k">제공일자</span><span class="v">2026.10.0{n}</span></li>'
            f'<li class="board-view-inner"><span class="k">담당자</span><span class="v">홍길동</span></li></ul>'
            f'<div class="board-view"><div class="content">{LONG}</div></div>'
            f'<ul class="view-file-list"><li>{name}<a class="ico_file" href="/cmm/fms/download/{n}.pdf">내려받기</a>'
            f'</li></ul></body></html>').encode()


DETAILS = {URLS[0]: detail(2, True), URLS[1]: detail(1, False)}  # second: filename from URL


class Enumeration(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, details=DETAILS):
        crawler, _ = load(SITE)
        captured, listing_calls = [], []

        def transport(url, **kwargs):
            if url in details:
                return details[url]
            listing_calls.append(url)
            return (LISTING if len(listing_calls) == 1 else EMPTY).encode()
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        crawler._curl_content_disposition_filename = lambda url, **kwargs: None  # header request fails
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured

    def adapter(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler), crawler

    def pipeline(self, details=DETAILS):
        p, adapter, crawler = self.adapter()
        url = LIST + '?menuNo=500498&pageIndex=1&pageUnit=30'
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, LISTING, url)).items
        self.assertEqual(URLS, [i.url for i in items])
        fetch = lambda u: Response(200, {'Content-Type': 'text/html'}, details[u], u)
        with contextlib.redirect_stdout(io.StringIO()):
            papers = [adapter.materialize(i, SimpleNamespace(get=fetch)) for i in items]
        self.assertNotIn('_curl_get', crawler.__dict__)
        return [x for x in papers if x is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['9002', '9001'], [p['external_id'] for p in expected])
        self.assertEqual(['보도2.pdf', '1.pdf'], [p['original_filename'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_abstract_skip_is_preserved(self):
        short = {u: v.replace(LONG.encode(), '짧음'.encode()) for u, v in DETAILS.items()}
        # crawl() pads short bodies with title/department/date context; compare whatever it keeps.
        expected = self.native(short)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(short))

    def test_empty_page_and_error_page_are_distinct(self):
        p, adapter, _ = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = LIST + '?menuNo=500498&pageIndex=103&pageUnit=30'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


if __name__ == '__main__':
    unittest.main()
