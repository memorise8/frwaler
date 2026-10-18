"""anses-fr-fr: enumeration split regression (collector-splits 방식 + ext:pdf_text).

crawl() 은 바꾸지 않았다. 합성 HTML·합성 PDF 바이트(fixture)로 원래 crawl() 이 저장하는
paper 와 enumeration 경로(_enumeration_items → PDF 상세 → 틀의 pdf_text → _enumeration_paper)가
내는 paper 가 같은지 확인한다. pdftotext 는 양쪽 모두 가짜(본문→텍스트 표)로 바꾸고, crawl() 이
pdftotext 에 넘기는 옵션·시간 제한이 plan 의 adapter.pdf_text 와 같은지도 본다.
네트워크·실제 프로세스·DB 없음.
"""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load, run_split, split_fixture

SITE = 'anses-fr-fr'


class EnumerationSplit(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())
        self.spec = self.plan['sites'][SITE]['adapter']['pdf_text']
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

    def fake_extract(self, fixture):
        """틀의 pdftext.extract 대역: 같은 spec 인지 확인하고 본문에 맞는 텍스트(없으면 None)."""
        def extract(body, spec):
            self.assertEqual(self.spec, spec)
            return fixture['pdf_texts'].get(body.decode('utf-8'))
        return patch('crawler.enumeration.pdftext.extract', extract)

    # -- 원래 crawl() 경로 ------------------------------------------------------
    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        saved, listing_calls, commands = [], [], []

        def curl_bytes(url, context='', referer=None, accept=None):
            if url in fixture['details']:
                return fixture['details'][url].encode('utf-8'), url
            listing_calls.append(url)
            body = fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
            return body.encode('utf-8'), url

        def fake_run(cmd, input=None, capture_output=False, timeout=None, **kwargs):
            commands.append((list(cmd), timeout))
            text = fixture['pdf_texts'].get((input or b'').decode('utf-8'))
            if text is None:
                return SimpleNamespace(returncode=1, stdout=b'', stderr=b'Syntax Error')
            return SimpleNamespace(returncode=0, stdout=text.encode('utf-8'), stderr=b'')
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._source_failure = None
        crawler._check_cancelled = lambda *a, **k: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        crawler._curl_get_bytes = curl_bytes
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module.subprocess, 'run', fake_run))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            result = crawler.crawl(limit=10)
        self.assertEqual(len(saved), result)
        self.assertEqual(1, len(listing_calls))  # no pager-next link: crawl() stops after page 0
        self.commands = commands
        return saved

    # -- enumeration 경로 ------------------------------------------------------
    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        ct = {'Content-Type': fixture['content_type']}
        items = parser.parse(Response(200, ct, fixture['listing_body'], fixture['listing_url'])).items
        self.assertEqual(2, len(items))
        bare = build_url(p.listing['url'], p.listing.get('params') or {}, drop={p.listing.get('page_param', 'page')})
        bare_items = parser.parse(Response(200, ct, fixture['listing_body'], bare)).items
        self.assertEqual([(i.key, i.url) for i in items], [(i.key, i.url) for i in bare_items])

        def fetch(url):
            self.assertIn(url, fixture['details'], 'undeclared detail request')
            return Response(200, {'Content-Type': 'application/pdf'}, fixture['details'][url], url)
        papers = []
        with self.fake_extract(fixture):
            for item in items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return papers

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(2, len(expected), 'fixture must save two papers through crawl()')
        actual = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(clean_paper(p)) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(2, len({paper_identity(p) for p in actual}))

    def test_plan_pdf_text_options_equal_crawl_first_pdftotext_call(self):
        self.native()
        cmd, timeout = self.commands[0]
        # crawl(): ["pdftotext", <options>, "-", "-"] (stdin → stdout); the framework passes a file path.
        self.assertEqual(['pdftotext'] + self.spec['args'] + ['-', '-'], cmd)
        self.assertEqual(self.spec['timeout_s'], timeout)
        self.assertEqual(0, self.spec.get('min_bytes', 0))
        # -layout produced text, so crawl() never needed its -raw fallback here.
        self.assertTrue(all('-layout' in c for c, _ in self.commands))

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Court.'))
        expected = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))

    def test_failed_extraction_is_an_item_failure_not_a_skip(self):
        _, adapter = self.adapter()
        with self.assertRaises(PermanentFailure) as ctx:
            adapter.crawler._enumeration_paper(None, 'https://www.anses.fr/system/files/X.pdf', {})
        self.assertEqual('pdf_text_extraction_failed', ctx.exception.reason)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        url = self.fixture['listing_url']
        page = parser.parse(Response(200, {'Content-Type': self.fixture['content_type']}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        err = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                    '<html><body><p>Site under maintenance</p></body></html>', url))
        self.assertTrue(err.extra.get('parse_error'))
        self.assertFalse(err.extra.get('explicit_empty', False))

    def test_shipped_plan_completes_end_to_end(self):
        end = lambda url: Response(200, {'Content-Type': self.fixture['content_type']}, self.fixture['empty_body'], url)
        with self.fake_extract(self.fixture), patch('crawler.enumeration.pdftext.available', lambda: True):
            result, docs = run_split(SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        tree = ast.parse(code)
        cls = next(x for x in tree.body if isinstance(x, ast.ClassDef) and any(
            isinstance(n, ast.FunctionDef) and n.name == 'crawl' for n in x.body))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual.get(name), name)


if __name__ == '__main__':
    unittest.main()
