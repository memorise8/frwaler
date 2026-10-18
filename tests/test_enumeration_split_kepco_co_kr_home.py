"""kepco-co-kr-home: POST list (ext:post_listing), GET boardView.do detail, PDF via
second_step.rewrite of the javascript:downloadFile link to GET FileDownload.do, ext:pdf_text.

Memory-only: crawl() (unchanged) is driven with a fake _curl_raw (its POST list/detail) and a fake
PDF text; the plan path gets the same PDF text from the framework's pdf_text step (pdftotext
replaced by the same fake). Network, subprocess and database I/O fail the test.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'kepco-co-kr-home'
ROOT = Path(__file__).resolve().parents[1]
FIX = json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text(encoding='utf-8'))
PLAN = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text(encoding='utf-8'))
H = 'https://www.kepco.co.kr'
LIST = H + '/home/media/newsroom/pr/boardListAjax.do'
VIEW_AJAX = H + '/home/media/newsroom/pr/boardViewAjax.do'
VIEW = H + '/home/media/newsroom/pr/boardView.do'
DOWNLOAD = H + '/c2r/FileDownload.do'
CT = {'Content-Type': 'text/html;charset=UTF-8'}


def fake_extract(fixture):
    def extract(body, spec):
        body = bytes(body)
        if not body.startswith(b'%PDF-'):
            return None
        return fixture['pdf_text_by_seq'].get(body[5:].decode())
    return extract


def download_body(fixture, url):
    q = parse_qs(urlsplit(url).query)
    return b'%PDF-' + q['fileSeq'][0].encode()


class KepcoSplit(unittest.TestCase):
    def setUp(self):
        self.denials = []
        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('split test attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=FIX):
        crawler, _ = load(SITE)
        captured, calls = [], []

        def curl_raw(url, method='GET', post_data=None, retries=3):
            calls.append((method, url, post_data))
            form = parse_qs(post_data or '')
            if url == LIST:
                return (fixture['listing_body'] if form['page'] == ['1'] else fixture['empty_body']).encode()
            if url == VIEW_AJAX:
                return fixture['detail_ajax'][form['boardNo'][0]].encode()
            raise AssertionError('unexpected request ' + url)

        crawler._curl_raw = curl_raw
        crawler._download_pdf_text = lambda no, seq: fixture['pdf_text_by_seq'].get(seq)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, fixture=FIX):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, CT, fixture['listing_body'], LIST)).items
        self.assertEqual(['3166', '3165', '3164'], [i.key for i in items])
        self.downloads = []

        def fetch(url):
            if url.startswith(VIEW + '?'):
                board = parse_qs(urlsplit(url).query)['boardNo'][0]
                return Response(200, CT, fixture['detail_page'][board], url)
            if url.startswith(DOWNLOAD + '?'):
                self.downloads.append(url)
                return Response(200, {'Content-Type': 'application/pdf'}, download_body(fixture, url), url)
            raise AssertionError('undeclared request ' + url)

        papers = []
        with patch('crawler.enumeration.pdftext.extract', fake_extract(fixture)):
            for item in items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['3166', '3164'], [p['post_number'] for p in expected])
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(f'{VIEW}?boardMngNo=15&boardNo=3166', actual[0]['url'])
        self.assertEqual('전자파 정보', actual[1]['title'])  # empty detail title -> list title, like crawl()

    def test_pdf_link_is_first_pdf_attachment_rewritten_to_get(self):
        self.pipeline()
        # Item 3165 has no PDF: second_step required=false, no download request.
        self.assertEqual(2, len(self.downloads))
        q = parse_qs(urlsplit(self.downloads[0]).query)
        self.assertEqual(['jK9Y+t20/Q=='], q['fileNo'])
        self.assertEqual(['gEeD+tuD/yg=='], q['fileSeq'])
        self.assertIn('fileSeq=gEeD%2BtuD%2Fyg%3D%3D', self.downloads[0])  # encoded once

    def test_short_pdf_text_skip_is_preserved(self):
        f = copy.deepcopy(FIX)
        f['pdf_text_by_seq'] = {k: 'Short.' for k in f['pdf_text_by_seq']}
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_missing_pdf_text_skip_is_preserved(self):
        f = copy.deepcopy(FIX)
        f['pdf_text_by_seq'] = {}
        self.assertEqual([], self.native(f))
        self.assertEqual([], self.pipeline(f))

    def test_empty_page_and_error_page_are_distinct(self):
        p = site_plan(PLAN, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, CT, FIX['empty_body'], LIST))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        try:
            bad = parser.parse(Response(200, CT, '<html><body>maintenance</body></html>', LIST))
        except Exception as exc:  # PermanentFailure listing_parse_error
            self.assertIn('listing_parse_error', str(getattr(exc, 'reason', exc)))
        else:
            self.assertTrue(bad.extra.get('parse_error'))
            self.assertFalse(bad.extra.get('explicit_empty', False))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in FIX['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_caller_input_is_not_mutated(self):
        before = copy.deepcopy(FIX)
        self.pipeline()
        self.assertEqual(before, FIX)


class PostTransport:
    def __init__(self, fixture, first_list=None):
        self.fixture, self.first_list, self.calls = fixture, first_list, []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, data))
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'', url)
        if method == 'POST' and url == LIST:
            form = parse_qs(unquote(data.decode() if isinstance(data, bytes) else data or ''))
            if form.get('page') == ['1']:
                return Response(200, CT, self.first_list or self.fixture['listing_body'], url)
            return Response(200, CT, self.fixture['empty_body'], url)
        if method == 'GET' and url.startswith(VIEW + '?'):
            board = parse_qs(parts.query)['boardNo'][0]
            return Response(200, CT, self.fixture['detail_page'][board], url)
        if method == 'GET' and url.startswith(DOWNLOAD + '?'):
            return Response(200, {'Content-Type': 'application/pdf'}, download_body(self.fixture, url), url)
        return Response(404, {}, b'', url)


class KepcoSplitRun(unittest.TestCase):
    def setUp(self):
        self.denials = []
        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('split run attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def run_plan(self, first_list=None):
        from tests.enumeration_fakes import Env
        plan = copy.deepcopy(PLAN['sites'][SITE])
        plan['request_interval_s'] = 0
        transport = PostTransport(FIX, first_list)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with patch('crawler.enumeration.pdftext.extract', fake_extract(FIX)), \
                    patch('crawler.enumeration.pdftext.available', lambda: True), \
                    contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, None, crawler=cls(env.conn), transport=transport)
            return result, env.docs(), transport
        finally:
            env.close()

    def test_shipped_plan_completes_end_to_end(self):
        result, docs, transport = self.run_plan()
        self.assertTrue(result['status'].startswith('complete'), result)
        self.assertEqual({'3166', '3164'}, {d['post_number'] for d in docs})
        self.assertTrue(any(m == 'POST' and u == LIST for m, u, _ in transport.calls))
        self.assertFalse(any(u.startswith(VIEW_AJAX) for _, u, _ in transport.calls))
        self.assertFalse(any(m == 'POST' and u.startswith(DOWNLOAD) for m, u, _ in transport.calls))

    def test_error_listing_ends_incomplete(self):
        result, docs, _ = self.run_plan('<html><body><p>Site under maintenance</p></body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
