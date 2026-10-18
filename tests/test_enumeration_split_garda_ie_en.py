"""L3b-20261018 (X2): enumeration split for garda-ie-en (ext:pdf_text).
The annual-reports start page lists PDFs in div.file-list (no sub-pages today).
crawl() downloads each PDF and runs `pdftotext -layout` (pypdf when empty); the
split lets the framework run the same pdftotext flags on the gated PDF response,
and the paper method keeps crawl()'s strip / pypdf step / 50-char rule.
Identity (PDF slug) unchanged. Synthetic HTML and PDFs; pdftotext runs locally."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import shutil
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import ROOT, load
from tests.enumeration_fakes import Env

SITE = 'garda-ie-en'
INDEX = 'https://www.garda.ie/en/information-centre/annual-reports/'
FOLDER = 'https://www.garda.ie/en/about-us/publications/annual%20reports/synthetic/'
PDF1 = FOLDER + '2031-synthetic-report.pdf'
PDF2 = FOLDER + '2032-synthetic-report.pdf'
LONG = ('Synthetic annual report text for a regression fixture describing a fictional year '
        'with enough words to pass the minimum abstract length rule of the crawler.')
ROW = ('<li><a href="/en/about-us/publications/annual reports/synthetic/{n}-synthetic-report.pdf" title="{n} Synthetic Report">'
       '<span class="file-name">{n} Synthetic Report</span></a><span class="file-date">0{d}/03/{n}</span>'
       '<span class="file-size">1.0Mb</span><span class="file-extension">pdf</span></li>')
LISTING = ('<html><body><main><div class="file-list"><ul>' + ROW.format(n=2031, d=1) + ROW.format(n=2032, d=2)
           + '</ul></div></main></body></html>')
SUBPAGE = '<div class="landing-document"><a href="/en/information-centre/annual-reports/older/">Older</a></div>'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


def make_pdf(text):
    """One-page PDF (Helvetica) with ``text`` wrapped over lines."""
    words, lines, cur = text.split(), [], ''
    for w in words:
        if len(cur) + len(w) + 1 > 70:
            lines.append(cur)
            cur = w
        else:
            cur = (cur + ' ' + w).strip()
    if cur:
        lines.append(cur)
    content = ('BT /F1 10 Tf 50 750 Td 14 TL ' + ' '.join(f'({x}) Tj T*' for x in lines) + ' ET').encode()
    objs = [b'<< /Type /Catalog /Pages 2 0 R >>', b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R '
            b'/Resources << /Font << /F1 5 0 R >> >> >>',
            b'<< /Length %d >>\nstream\n' % len(content) + content + b'\nendstream',
            b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offsets = bytearray(b'%PDF-1.4\n'), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % n + body + b'\nendobj\n'
    xref = len(out)
    out += b'xref\n0 %d\n0000000000 65535 f \n' % (len(objs) + 1)
    out += b''.join(b'%010d 00000 n \n' % o for o in offsets)
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (len(objs) + 1, xref)
    return bytes(out)


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


@unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext not installed')
class RegisteredSplit(unittest.TestCase):
    def pdfs(self, text=LONG):
        return {PDF1: make_pdf(text), PDF2: make_pdf(text.replace('year', 'period'))}

    def native(self, pdfs, listing=LISTING):
        crawler, module = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def get(url, retries=3, binary=True):
            if url in pdfs:
                return pdfs[url]
            return listing if url == INDEX else None
        with patch.object(module, '_curl_get', get), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, pdfs, redirect=False):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        items = parser.parse(Response(200, {'Content-Type': 'text/html'}, LISTING, build_url(INDEX, {'page': 1}))).items
        bare = parser.parse(Response(200, {'Content-Type': 'text/html'}, LISTING, INDEX)).items
        self.assertEqual([i.key for i in items], [i.key for i in bare])
        calls = []

        def fetch(url):
            self.assertIn(url, pdfs, 'undeclared detail request')
            calls.append(url)
            return Response(200, {'Content-Type': 'application/pdf'}, pdfs[url], url + '?moved=1' if redirect else url)
        papers = [adapter.materialize(item, SimpleNamespace(get=fetch)) for item in items]
        self.assertEqual([PDF1, PDF2], calls)
        return [x for x in papers if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(self.pdfs())
        self.assertEqual(2, len(expected))
        actual = self.pipeline(self.pdfs())
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in actual])
        self.assertEqual(['2031-synthetic-report', '2032-synthetic-report'], [x['external_id'] for x in actual])
        self.assertEqual(['2031-03-01', '2032-03-02'], [x['published_date'] for x in actual])
        self.assertTrue(actual[0]['abstract'].startswith('Synthetic annual report'))

    def test_detail_redirect_preserves_registered_identity(self):
        self.assertEqual([clean_paper(x) for x in self.native(self.pdfs())], self.pipeline(self.pdfs(), redirect=True))

    def test_short_or_unreadable_pdf_is_skipped_like_crawl(self):
        for pdfs in (self.pdfs('Too short.'), {PDF1: b'<html>error</html>', PDF2: make_pdf('Short')}):
            self.assertEqual([], self.native(pdfs))
            self.assertEqual([], self.pipeline(pdfs))

    def test_subpages_stop_the_listing_instead_of_being_missed(self):
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(LISTING.replace('<main>', '<main>' + SUBPAGE), INDEX)
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_page_above_one_is_explicit_empty_and_error_page_is_not(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page2 = build_url(INDEX, {'page': 2})
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, LISTING, page2))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, page2))
        self.assertFalse(foreign.extra.get('explicit_empty', False))
        for url in (INDEX, build_url(INDEX, {'page': 1})):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_original_code_unchanged(self):
        """crawl(), the methods and module helpers it uses keep their source (hashes at the split's base)."""
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        actual = {}
        for n in ast.parse(code).body:
            if isinstance(n, ast.FunctionDef):
                actual[n.name] = hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, ast.FunctionDef):
                        actual[f'{n.name}.{m.name}'] = hashlib.sha256(ast.get_source_segment(code, m).encode()).hexdigest()
        for name, digest in ORIGINAL_SHA256.items():
            self.assertEqual(digest, actual.get(name), name)
    def test_end_to_end_run_completes(self):
        pdfs = self.pdfs()
        site = copy.deepcopy(plan()['sites'][SITE])
        site['request_interval_s'] = 0

        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url in pdfs:
                return Response(200, {'Content-Type': 'application/pdf'}, pdfs[url], url)
            return Response(200, {'Content-Type': 'text/html'}, LISTING, url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'2031-synthetic-report', '2032-synthetic-report'}, {d['post_number'] for d in docs})


ORIGINAL_SHA256 = {'_curl_get': '822e04b58c3e4ea75725af03e2bcd87cb44bc166e12f0a9c938d318adb7f5b7a', '_make_soup': 'f5432be970a27a2751d2cec0be6a619c0de74012bd68c3be6beb025f43cdf8c6', '_pdf_text': 'ade3ef85520ee5fca060812d06ffd60477dc4bcb333e36c7d8dd4573f0bc5e8a', '_parse_date': 'a5e1e3d632a5085ba2c3b5072463b9177ee5e4b058adc26e4ecf6e4ed6bb5d56', '_parse_page': 'd3069eac870a23dac8ff737b6565dc45eaf024ad46ea856bd8db5158a711cae3', 'GardaIeEnCrawler.crawl': '9ddb1b90b4f410c5ac6da0b1f1eee7f522aa927d0897a5c9a4ac3b051bcdfb9f'}

if __name__ == '__main__':
    unittest.main()
