"""L3b-20261018: enumeration split for met-no-publikasjoner.

List: the main page /publikasjoner/met-report holds the current year, each earlier
year has /publikasjoner/met-report/met-report-YYYY (no pagination; ?page>1 is
declared empty and proved by #main-container). Each row is a PDF; the abstract is
the PDF's Abstract/Sammendrag section (pdfplumber, then pypdf) as in crawl().
Plan: date_range_split with explicit segments (main page + one per year page).
Memory-only: PDFs are generated here from fixture text; no network or subprocess."""
import contextlib
import copy
import datetime
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
from tests.collector_split_helpers import ROOT, load

SITE = 'met-no-publikasjoner'
MAIN = 'https://www.met.no/publikasjoner/met-report'
Y2025 = 'https://www.met.no/publikasjoner/met-report/met-report-2025'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


def make_pdf(text):
    """A one-page PDF (Helvetica text) that pdfplumber and pypdf both read."""
    words, lines, cur = text.split(), [], ''
    for w in words:
        if w == '1.':
            lines.append(cur)
            cur = w
        elif len(cur) + len(w) + 1 > 70:
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
    fx = json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())
    cur = str(datetime.datetime.now().year)
    fx['main_body'] = fx['main_body'].replace('CUR', cur)
    return fx


def plan_raw():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


def pdf_urls(html):
    return ['https://www.met.no' + h for h in re.findall(r'href="([^"]+\.pdf)"', html)]


class MetSplit(unittest.TestCase):
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

    def pages(self):
        return {MAIN: self.fx['main_body'], Y2025: self.fx['listings']['2025']}

    def pdf(self, url, text=None):
        return make_pdf(text or self.fx['pdf_long_text'])

    def native(self, text=None):
        crawler, _ = load(SITE)
        captured = []
        pages = self.pages()

        def curl(url, *, binary=False, retries=3):
            if url.endswith('.pdf'):
                return self.pdf(url, text)
            return pages.get(url)
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = curl
        crawler._pdf_size = lambda url: None
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def adapter_parser(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        return p, adapter, ListingParser(p.listing['parser'], adapter=adapter)

    def pipeline(self, text=None, redirect=False):
        p, adapter, parser = self.adapter_parser()
        papers, keys = [], []
        for seg in p.raw['segments']['items'][:2]:
            url = p.listing['url'].replace('{segment}', seg['key']) + '?page=1'
            body = self.pages()[url.split('?')[0]]
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url))
            keys += page.keys

            def fetch(u):
                self.assertTrue(u.endswith('.pdf'))
                return Response(200, {'Content-Type': 'application/pdf'}, self.pdf(u, text),
                                u + '?moved=1' if redirect else u)
            for item in page.items:
                paper = adapter.materialize(item, SimpleNamespace(get=fetch))
                if paper is not None:
                    papers.append(paper)
        return keys, papers

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        cur = datetime.datetime.now().year
        ids = [f'met-report-02-{cur}', f'met-report-01-{cur}', 'met-report-07-2025', 'met-report-03-2025']
        self.assertEqual(ids, [p['external_id'] for p in expected])
        keys, actual = self.pipeline()
        self.assertEqual(ids, keys)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual([MAIN, MAIN, Y2025, Y2025], [p['url'] for p in actual])
        self.assertTrue(all(len(p['abstract']) >= 50 for p in actual))

    def test_detail_redirect_preserves_registered_identity(self):
        self.assertEqual([clean_paper(p) for p in self.native()], self.pipeline(redirect=True)[1])

    def test_no_abstract_section_skip_is_preserved(self):
        text = 'Synthetic report without the summary heading. ' * 5
        self.assertEqual([], self.native(text))
        self.assertEqual([], self.pipeline(text)[1])

    def test_oversized_pdf_is_skipped_like_crawl(self):
        _, adapter, parser = self.adapter_parser()
        crawler, _ = load(SITE)
        row = crawler._enumeration_items(self.fx['listings']['2025'], Y2025)[0]
        big = self.pdf(None) + b'%' * (crawler._MAX_PDF_BYTES + 1)
        with guarded():
            self.assertIsNone(crawler._enumeration_paper(big, row))

    def test_year_pages_declare_page_two_empty_and_errors_differ(self):
        p, _, parser = self.adapter_parser()
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fx['listings']['2025'], Y2025 + '?page=2'))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, Y2025 + '?page=1'))
        self.assertEqual([], foreign.items)
        self.assertFalse(foreign.extra.get('explicit_empty', False))
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items({'error': 'x'}, Y2025)

    def test_segments_cover_main_page_and_every_linked_year(self):
        keys = [s['key'] for s in self.plan['sites'][SITE]['segments']['items']]
        self.assertEqual('met-report', keys[0])
        self.assertEqual([f'met-report/met-report-{y}' for y in range(2025, 1982, -1)], keys[1:])

    def test_all_original_methods_are_unchanged(self):
        import ast
        import hashlib
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)


class MetSplitRun(unittest.TestCase):
    """The shipped plan through the real SiteRunner.run (temporary SQLite, fake transport)."""

    def test_shipped_plan_completes_end_to_end(self):
        from tests.enumeration_fakes import Env
        fx = fixture()
        plan = copy.deepcopy(plan_raw()['sites'][SITE])
        plan['request_interval_s'] = 0
        plan['segments']['items'] = plan['segments']['items'][:2]
        pages = {MAIN: fx['main_body'], Y2025: fx['listings']['2025']}

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if parts.path.endswith('.pdf'):
                return Response(200, {'Content-Type': 'application/pdf'}, make_pdf(fx['pdf_long_text']), url)
            base = url.split('?')[0]
            return Response(200, {'Content-Type': 'text/html'}, pages.get(base, fx['empty_body']), url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            crawler = cls(env.conn)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=crawler)
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(4, len(docs))
        self.assertEqual(4, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
