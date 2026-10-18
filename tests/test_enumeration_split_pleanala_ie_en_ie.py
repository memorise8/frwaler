"""L3b-20261018: enumeration split for pleanala-ie-en-ie.
crawl() reads three pages: the annual index (media links; its one HTML link is the 2019
sub-index, which crawl() expands), the 2019 sub-index (HTML table pages and media) and
the quarterly index (media). Plan: date_range_split with these three explicit segments
of /en-IE/Statistics/{segment}; ?page=N is ignored by the site, so page 2 is declared
empty (listing_marker article). Detail = the item GET (HTML page or file; the file's GET
headers stand in for crawl()'s HEAD). Memory-only: no network or subprocess (the
end-to-end run uses a temporary SQLite)."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load

SITE = 'pleanala-ie-en-ie'
PREFIX = 'https://www.pleanala.ie/en-IE/Statistics/'


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_raw():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class PleanalaSplit(unittest.TestCase):
    def setUp(self):
        self.fx = fixture()
        self.plan = plan_raw()
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child', 'sqlite3.connect'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fx=None):
        fx = fx or self.fx
        crawler, _ = load(SITE)
        captured = []

        def get_bytes(url, *, method='GET', accept='', timeout=30):
            if url in fx['files']:
                f = fx['files'][url]
                if method == 'HEAD':
                    return ('HTTP/1.1 200 OK\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in f['headers'].items()) + '\r\n').encode()
                return f['body'].encode()
            if url in fx['html_details']:
                return fx['html_details'][url].encode()
            key = url[len(PREFIX):] if url.startswith(PREFIX) else None
            return fx['pages'][key].encode() if key in fx['pages'] else None
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._get_bytes = get_bytes
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def adapter_parser(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        return p, adapter, ListingParser(p.listing['parser'], adapter=adapter)

    def pipeline(self, fx=None):
        fx = fx or self.fx
        p, adapter, parser = self.adapter_parser()
        papers = []

        def fetch(u):
            if u in fx['files']:
                f = fx['files'][u]
                return Response(200, f['headers'], f['body'].encode(), u)
            self.assertIn(u, fx['html_details'])
            return Response(200, {'Content-Type': 'text/html'}, fx['html_details'][u], u)
        for seg in [i['key'] for i in self.plan['sites'][SITE]['segments']['items']]:
            url = p.listing['url'].replace('{segment}', seg) + '?page=1'
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, fx['pages'][seg], url))
            for item in page.items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return papers

    @staticmethod
    def by_id(papers):
        return sorted(papers, key=lambda p: p['external_id'])

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(3, len(expected))
        actual = self.pipeline()
        self.assertEqual(self.by_id([clean_paper(p) for p in expected]), self.by_id(actual))
        self.assertEqual(sorted(paper_identity(p) for p in expected), sorted(paper_identity(p) for p in actual))
        self.assertEqual(['11111111-2222-3333-4444-555555555555', 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
                          'ar2019-appendix-01-synthetic-table'], sorted(p['external_id'] for p in actual))

    def test_short_abstract_skip_is_preserved(self):
        fx = json.loads(json.dumps(self.fx, ensure_ascii=False).replace(self.fx['long_text'], 'Short.'))
        self.assertEqual(2, len(self.native(fx)))  # the HTML table page is skipped, media keep the fallback
        self.assertEqual(sorted(p['external_id'] for p in self.native(fx)),
                         sorted(p['external_id'] for p in self.pipeline(fx)))

    def test_all_original_methods_are_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_page_two_empty_error_page_and_unknown_sub_index(self):
        p, adapter, parser = self.adapter_parser()
        url = PREFIX + 'Quarterly-Statistics?page=2'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fx['pages']['Quarterly-Statistics'], url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        error = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', PREFIX + 'Quarterly-Statistics?page=1'))
        self.assertTrue(error.extra.get('parse_error'))
        crawler, _ = load(SITE)
        changed = self.fx['pages']['Annual-Statistics'].replace('2019-Appendices', '2027-Appendices')
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items(changed, PREFIX + 'Annual-Statistics?page=1')
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items(changed, PREFIX + 'Other-Page?page=1')

    def test_segments_match_crawler_pages(self):
        crawler, _ = load(SITE)
        keys = [i['key'] for i in self.plan['sites'][SITE]['segments']['items']]
        self.assertEqual(PREFIX + keys[0], crawler._START_URL)
        self.assertEqual(PREFIX + keys[2], crawler._QUARTERLY_URL)
        self.assertEqual(list(crawler._ENUMERATION_SUBINDEX), [keys[1]])


class PleanalaRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fx = fixture()
        plan = copy.deepcopy(plan_raw()['sites'][SITE])
        plan['request_interval_s'] = 0

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fx['files']:
                f = fx['files'][url]
                return Response(200, f['headers'], f['body'].encode(), url)
            if url in fx['html_details']:
                return Response(200, {'Content-Type': 'text/html'}, fx['html_details'][url], url)
            key = parts.path[len('/en-IE/Statistics/'):]
            if key in fx['pages']:  # the site ignores ?page=N
                return Response(200, {'Content-Type': 'text/html'}, fx['pages'][key], url)
            return Response(404, {'Content-Type': 'text/html'}, '<html>404</html>', url)

        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(3, len(docs))


if __name__ == '__main__':
    unittest.main()
