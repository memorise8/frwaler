"""bmleh-de-en: enumeration split (RV 2026-10-19).
One static list page (?page is ignored, page 2 repeats page 1 -> wrap, last page 1). Rows are
detail_pdf (detail page + its download PDF, second step), basepage (detail page only) and
direct_pdf (the row links the PDF itself). _enumeration_paper runs the unchanged
_build_document on the bodies the runner fetched (its self._curl answered from them), so the
paper is crawl()'s save argument; a request crawl() would make that the runner did not make
fails the item. crawl() is unchanged (file pinned by delivery/incremental_holds.json, hash
updated with it). Memory only: no network, process or customer DB."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load, split_fixture, split_plan

SITE = 'bmleh-de-en'
LIST = 'https://www.bmleh.de/EN/ministry/organisation/advisory-boards/AgriculturalPolicyPublications.html'


def body_of(fixture, url):
    if url.split('?page=')[0] == LIST:
        return 'text/html; charset=utf-8', fixture['listing_body'].encode()
    if url in fixture['pages']:
        return 'text/html; charset=utf-8', fixture['pages'][url].encode()
    if url in fixture['pdfs']:
        return 'application/pdf', fixture['pdfs'][url].encode('latin-1')
    return None, None


class Split(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        self.plan = {'sites': {SITE: split_plan(SITE)}}
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fixture=None):
        """The unchanged crawl(), its self._curl answered from the fixture."""
        fixture = fixture or self.fixture
        crawler, _ = load(SITE)
        captured, calls = [], []

        def curl(url, *, binary=False, retries=3):
            calls.append((url, binary))
            _, body = body_of(fixture, url)
            if body is None:
                return None
            return body if binary else body.decode('utf-8', errors='replace')
        crawler._curl = curl
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl()
        return captured, calls

    def adapter(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        crawler._delay = 0  # set by the constructor in a real run
        return p, Adapter(SITE, p.section('adapter'), None, crawler=crawler)

    def pipeline(self, fixture=None):
        fixture = fixture or self.fixture
        p, adapter = self.adapter()
        items = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'text/html'}, fixture['listing_body'], LIST + '?page=1')).items
        self.assertEqual(3, len(items))  # the duplicate row (crawl()'s seen_urls) is dropped by key de-duplication
        seen, papers, calls = set(), [], []

        def get(u):
            calls.append(u)
            ctype, body = body_of(fixture, u)
            self.assertIsNotNone(body, 'undeclared request ' + u)
            return Response(200, {'Content-Type': ctype}, body, u)
        for item in items:
            if item.key in seen:
                continue
            seen.add(item.key)
            paper = adapter.materialize(item, SimpleNamespace(get=get))
            if paper is not None:
                papers.append(paper)
        return papers, calls

    def test_paper_and_identity_equal_original_crawl(self):
        expected, calls = self.native()
        self.assertEqual(['synthetic-position-paper', 'synthetic-statement', 'synthetic-joint-statement'],
                         [p['post_number'] for p in expected])
        self.assertIn('Executive Summary', ''.join(self.fixture['pdfs'].values()))
        self.assertTrue(expected[0]['abstract'].startswith('This synthetic summary text'))  # from the PDF
        actual, requests = self.pipeline()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(sorted({u for u, _ in calls} - {LIST}), sorted(set(requests)))  # same requests as crawl()

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.')
                       .replace(self.fixture['long_text_2'], 'Short.'))
        expected, _ = self.native(f)
        actual, _ = self.pipeline(f)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual(['synthetic-statement'], [p['post_number'] for p in actual])  # PDF rows skipped

    def test_request_the_runner_did_not_make_fails_the_item(self):
        crawler, _ = load(SITE)
        crawler._delay = 0
        item = {'kind': 'detail_pdf', 'href': 'https://www.bmleh.de/x.html', 'title': 'X', 'trailing': ''}
        page = Response(200, {}, self.fixture['pages'][
            'https://www.bmleh.de/SharedDocs/Downloads/EN/Ministry/synthetic-position-paper.html'].encode(), item['href'])
        with self.assertRaises(RuntimeError):
            crawler._enumeration_paper(item, page, page)  # the PDF link was not fetched

    def test_listing_rejects_wrong_shape_and_empty_page_needs_marker(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'error': 'not a listing'})
        p, adapter = self.adapter()
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><div id="content"></div></html>', LIST))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        try:
            bad = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', LIST))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertTrue(bad.extra.get('parse_error'))

    def test_original_methods_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


class SplitRun(unittest.TestCase):
    """The shipped plan end to end through the real SiteRunner: ?page=2 repeats the page (wrap)."""

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = dict(split_plan(SITE), request_interval_s=0)

        def handler(url):
            if urlsplit(url).path == '/robots.txt':
                return Response(404, {}, b'', url)
            ctype, body = body_of(fixture, url)
            if body is None:
                return Response(404, {}, b'', url)
            return Response(200, {'Content-Type': ctype}, body, url)
        env = Env(SITE)
        try:
            cls = type(load(SITE)[0])
            with contextlib.redirect_stdout(io.StringIO()), patch('time.sleep', lambda *a: None):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['synthetic-joint-statement', 'synthetic-position-paper', 'synthetic-statement'],
                         sorted(d['post_number'] for d in docs))


if __name__ == '__main__':
    unittest.main()
