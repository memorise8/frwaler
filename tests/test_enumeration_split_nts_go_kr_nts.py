"""nts-go-kr-nts: enumeration split regression (collector-splits 방식, 틀 확장 X2).

crawl() 은 바꾸지 않았다. 합성 HTML/JSON(fixture)으로 원래 crawl() 이 저장하는 paper 와
enumeration 경로가 내는 paper 가 같은지 확인한다:
  목록 GET → _enumeration_items (ebook 링크 있는 행만)
  1단계 상세 GET selectNttInfo.do (first_html, bbsV_cont 본문)
  2단계 second_step.url_template → selectNttFileList.do JSON (html, 파일 목록)
  → _enumeration_paper (external_id=nttSn, url=상세 주소, 초록·pdf_url·metadata 동일).
crawl() 은 목록·상세를 POST 로 받지만 GET 도 같은 항목·본문을 준다(실접속 확인, 증거
work/L2a/evidence/nts-go-kr-nts.x2*.json). 네트워크·프로세스·실제 DB 없음.
"""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture

SITE = 'nts-go-kr-nts'
DETAIL = 'https://www.nts.go.kr/nts/na/ntt/selectNttInfo.do?nttSn={}&mi=2210'
FILES = 'https://www.nts.go.kr/nts/na/ntt/selectNttFileList.do?nttSn={}&bbsId=1034&mi=2210'


def body_of(value):
    return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value


class EnumerationSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('io')
            raise AssertionError('split test attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        """Unchanged crawl(): list/detail via _curl_post, file list via _curl_get."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        saved, list_pages = [], []

        def post(url, data):
            if url.endswith('/selectNttList.do'):
                list_pages.append(data['currPage'])
                return fixture['listing_body'] if data['currPage'] == '1' else fixture['empty_body']
            self.assertEqual({'bbsId': '1034', 'mi': '2210', 'currPage': '1'},
                             {k: v for k, v in data.items() if k != 'nttSn'})
            return body_of(fixture['details'].get(DETAIL.format(data['nttSn']), ''))

        def get(url):
            return body_of(fixture['details'].get(url, '')) or None
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        crawler._curl_post = post
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(saved), result)
        self.assertEqual(['1', '2'], list_pages)  # page 2 has rows but no ebook link → crawl() stops
        return saved

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        resp = Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], fixture['listing_url'])
        items = parser.parse(resp).items
        self.assertEqual(['1353204', '1350463'], [i.key for i in items])
        self.assertEqual([DETAIL.format('1353204'), DETAIL.format('1350463')], [i.url for i in items])
        calls = []

        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            calls.append(url)
            value = fixture['details'][url]
            ctype = 'text/html' if '/selectNttInfo.do' in url else 'text/plain'  # site sends no JSON type
            return Response(200, {'Content-Type': ctype}, body_of(value), url)
        papers = []
        for item in items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        # each item: first the detail page, then the file-list JSON built from the item key
        self.assertEqual([DETAIL.format('1353204'), FILES.format('1353204'),
                          DETAIL.format('1350463'), FILES.format('1350463')], calls)
        return papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected), 'fixture must save two papers through crawl()')
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(clean_paper(p)) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(['1353204', '1350463'], [p['external_id'] for p in actual])
        self.assertEqual([DETAIL.format('1353204'), DETAIL.format('1350463')], [p['url'] for p in actual])
        self.assertEqual('https://www.nts.go.kr/comm/nttFileDownload.do?fileKey=43007633f2624383fdb22cfb135d19bc',
                         actual[0]['pdf_url'])
        self.assertIn('첨부파일: 2026년 국세 여름호.pdf (134.9MB).', actual[0]['abstract'])
        self.assertEqual('https://www.nts.go.kr/comm/nttFileDownload.do?fileKey=p2', actual[1]['pdf_url'])
        self.assertIn('세법 개정 해설', actual[1]['abstract'])
        meta = json.loads(actual[0]['metadata'])
        self.assertEqual(['fileNm', 'fileSize', 'dwldUrl', 'nttExtsn', 'fileTy'], list(meta['files'][0]))

    def test_broken_file_json_and_empty_detail_match_crawl(self):
        f = copy.deepcopy(self.fixture)
        for url in list(f['details']):
            f['details'][url] = 'not json' if '/selectNttFileList.do' in url else '<html><body>오류</body></html>'
        expected = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        self.assertTrue(all(p['pdf_url'] == '' for p in expected))

    def test_rows_without_ebook_link_are_an_empty_page(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = self.fixture['listing_url']
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        try:
            err = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                        '<html><body><p>점검 중입니다</p></body></html>', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertFalse(err.extra.get('explicit_empty', False))

    def test_end_to_end_run_through_site_runner(self):
        from tests.enumeration_fakes import Env
        plan = copy.deepcopy(self.plan['sites'][SITE])
        plan['request_interval_s'] = 0
        fixture = self.fixture
        seen = []

        def handler(url):
            parts = urlsplit(url)
            seen.append(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fixture['details']:
                return Response(200, {'Content-Type': 'text/html'}, body_of(fixture['details'][url]), url)
            if parts.path.endswith('/selectNttList.do'):
                page = int(parse_qs(parts.query).get('currPage', ['1'])[0])
                return Response(200, {'Content-Type': 'text/html'},
                                fixture['listing_body'] if page <= 1 else fixture['empty_body'], url)
            return Response(404, {}, b'')
        env = Env(SITE)
        self.addCleanup(env.close)
        _, module = load(SITE)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
        try:
            crawler = cls(env.conn, 0)
        except TypeError:
            crawler = cls(env.conn)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler, crawler=crawler)
        self.assertEqual('complete', result['status'], result)
        docs = env.docs()
        self.assertEqual({'1353204', '1350463'}, {d['post_number'] for d in docs})
        self.assertEqual({DETAIL.format('1353204'), DETAIL.format('1350463')}, {d['meta_url'] for d in docs})
        self.assertIn(FILES.format('1350463'), seen)


if __name__ == '__main__':
    unittest.main()
