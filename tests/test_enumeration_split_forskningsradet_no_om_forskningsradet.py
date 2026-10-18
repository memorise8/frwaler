"""L3b-20261018: enumeration split for forskningsradet-no-om-forskningsradet.

List: /om-forskningsradet/publikasjoner/?year=YYYY (&page=N, 100 rows per page; React
props "PublicationsPage" -> SearchResult rows; a page past the end keeps the page frame
.grouped-search-page with no rows). Each row is a PDF; the abstract is the PDF text.
Plan: date_range_split, yearly segments from 1996 (no splitting), pages read forward.
Memory-only: PDFs are generated here from fixture text; no network or subprocess."""
import contextlib
import copy
import io
import json
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load

SITE = 'forskningsradet-no-om-forskningsradet'
LIST_URL = 'https://www.forskningsradet.no/om-forskningsradet/publikasjoner/'


def make_pdf(text):
    """A one-page PDF (Helvetica text) that pdfminer and pypdf both read."""
    words, lines, cur = text.split(), [], ''
    for w in words:
        if len(cur) + len(w) + 1 > 70:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)
    esc = lambda s: s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    content = 'BT /F1 10 Tf 50 750 Td 14 TL ' + ' '.join(f'({esc(x)}) Tj T*' for x in lines) + ' ET'
    objs = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
            '/Resources << /Font << /F1 5 0 R >> >> >>',
            f'<< /Length {len(content)} >>\nstream\n{content}\nendstream',
            '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offs = b'%PDF-1.4\n', []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += f'{i} 0 obj\n{o}\nendobj\n'.encode('latin-1')
    xref = len(out)
    out += f'xref\n0 {len(objs) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offs)
    out += f'trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    return out


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_raw():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


def year_of(url):
    return (parse_qs(urlsplit(url).query).get('year') or [None])[0]


def page_of(url):
    return int((parse_qs(urlsplit(url).query).get('page') or ['1'])[0])


class ForskningsradetSplit(unittest.TestCase):
    def setUp(self):
        self.fx = fixture()
        self.plan = plan_raw()
        self.denials = []

        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('attempted network/process/database I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.no_db = patch('sqlite3.connect', denied)
        self.no_db.start()
        self.addCleanup(lambda: self.no_db.stop() if self.no_db else None)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def native(self, fx=None):
        fx = fx or self.fx
        crawler, _ = load(SITE)
        captured = []

        def curl(url, *, timeout=60, binary=False):
            if url in fx['pdf_texts']:
                return make_pdf(fx['pdf_texts'][url])
            year = year_of(url)
            return fx['listings'].get(year, '<html></html>') if year else fx['main_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = curl
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def adapter_parser(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        return p, adapter, ListingParser(p.listing['parser'], adapter=adapter)

    def pipeline(self, year, fx=None, redirect=False):
        fx = fx or self.fx
        p, adapter, parser = self.adapter_parser()
        url = build_url(p.listing['url'], {'year': year, 'page': 1})
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, fx['listings'][year], url))
        papers = []

        def fetch(u):
            self.assertIn(u, fx['pdf_texts'])
            return Response(200, {'Content-Type': 'application/pdf'}, make_pdf(fx['pdf_texts'][u]),
                            u + '?moved=1' if redirect else u)
        for item in page.items:
            paper = adapter.materialize(item, SimpleNamespace(get=fetch))
            if paper is not None:
                papers.append(paper)
        return page, papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['syntetisk-rapport-en', 'syntetisk-rapport-null'], [p['external_id'] for p in expected])
        page, actual = self.pipeline('2015')
        self.assertEqual(['syntetisk-rapport-en', 'syntetisk-rapport-null'], page.keys)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_short_pdf_text_skip_is_preserved(self):
        fx = json.loads(json.dumps(self.fx, ensure_ascii=False).replace(self.fx['long_text'], 'Short.'))
        self.assertEqual([], self.native(fx))
        self.assertEqual([], self.pipeline('2015', fx)[1])

    def test_detail_redirect_preserves_registered_identity(self):
        expected = self.native()
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline('2015', redirect=True)[1])

    def test_all_original_methods_are_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_empty_page_error_page_and_wrong_shape(self):
        p, adapter, parser = self.adapter_parser()
        url = LIST_URL + '?year=2015&page=2'
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fx['empty_body'], url))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items({'error': 'not a listing'}, url)

    def test_year_segments_start_1996(self):
        from datetime import date
        from crawler.enumeration.strategies import generate_segments
        p = site_plan(self.plan, SITE, registered=[SITE])
        keys = generate_segments(p.section('segments'), date(2026, 10, 18))
        self.assertEqual('1996', keys[0])
        self.assertEqual('2026', keys[-1])
        self.assertEqual(31, len(keys))

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fx = self.fx
        plan = copy.deepcopy(self.plan['sites'][SITE])
        plan['request_interval_s'] = 0
        plan['segments']['from'] = '2015'
        plan['segments']['to'] = '2016'

        def handler(url):
            if urlsplit(url).path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fx['pdf_texts']:
                return Response(200, {'Content-Type': 'application/pdf'}, make_pdf(fx['pdf_texts'][url]), url)
            year = year_of(url)
            if year in fx['listings'] and page_of(url) == 1:
                return Response(200, {'Content-Type': 'text/html'}, fx['listings'][year], url)
            return Response(200, {'Content-Type': 'text/html'}, fx['empty_body'], url)

        self.no_db.stop()
        self.no_db = None
        env = Env(SITE)
        try:
            crawler = type(load(SITE)[0])(env.conn)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=crawler, today='2026-10-18')
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual({'syntetisk-rapport-en', 'syntetisk-rapport-null', 'syntetisk-2016'},
                         {d['post_number'] for d in docs})


if __name__ == '__main__':
    unittest.main()
