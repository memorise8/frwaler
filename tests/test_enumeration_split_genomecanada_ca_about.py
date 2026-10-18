"""L3b-20261018 (X2): enumeration split for genomecanada-ca-about (ext:pdf_text).
The single publications page lists PDFs; crawl() downloads each PDF and runs
`pdftotext -l 5` itself. The split lets the framework run the same pdftotext
flags (adapter.pdf_text) on the PDF fetched through the gate; the paper method
applies crawl()'s _clean / minimum-length rule. Identity (PDF filename stem) unchanged.
Synthetic HTML and PDFs only; pdftotext runs locally (no network)."""
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

SITE = 'genomecanada-ca-about'
INDEX = 'https://genomecanada.ca/about/publications/'
PDF1 = 'https://genomecanada.ca/wp-content/uploads/2031/07/Synthetic-Annual-Report-2030-31.pdf'
PDF2 = 'https://genomecanada.ca/wp-content/uploads/2031/02/Synthetic-Policy-Brief.pdf'
LONG = ('Synthetic report text for a regression fixture describing a fictional programme '
        'with enough words to pass the minimum abstract length rule of the crawler.')
LISTING = f'''<html><body><main>
<ul class="elementor-icon-list-items">
 <li class="elementor-icon-list-item"><a href="{PDF1}"><span class="elementor-icon-list-text">2030-2031</span></a></li>
</ul>
<section class="elementor-section"><div class="elementor-column"><h3>Synthetic policy brief on genomics</h3>
 <a href="{PDF2}">Download Brief</a></div></section>
</main></body></html>'''
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
        return {PDF1: make_pdf(text), PDF2: make_pdf(text.replace('report', 'brief'))}

    def native(self, pdfs):
        crawler, _ = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = lambda url, binary=False: pdfs.get(url) if binary else LISTING
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
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
        self.assertEqual(['Synthetic-Annual-Report-2030-31', 'Synthetic-Policy-Brief'], [x['external_id'] for x in actual])
        self.assertEqual(['203107', '203102'], [x['post_number'] for x in actual])
        self.assertTrue(actual[0]['abstract'].startswith('Synthetic report text'))

    def test_detail_redirect_preserves_registered_identity(self):
        self.assertEqual([clean_paper(x) for x in self.native(self.pdfs())], self.pipeline(self.pdfs(), redirect=True))

    def test_short_or_unreadable_pdf_is_skipped_like_crawl(self):
        for pdfs in (self.pdfs('Too short.'), {PDF1: b'<html>error</html>', PDF2: make_pdf('Short')}):
            self.assertEqual([], self.native(pdfs))
            self.assertEqual([], self.pipeline(pdfs))

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
        self.assertEqual({'Synthetic-Annual-Report-2030-31', 'Synthetic-Policy-Brief'}, {d['post_number'] for d in docs})


ORIGINAL_SHA256 = {'_make_soup': 'e9a542b8715fbba06ff7e0955ed6dd407804658f1fb9022037301df877bcbcb2', '_clean': 'dbb071ac48aa28161ab3e829a3aeb49c9c4cc6d058cf6582a358dbf284fffd64', '_fn_to_title': 'd25a210af4ab7952c296cbba7509e5ea6fb81e57786b7923bf91feccf9130cfe', '_upload_date': 'cbc8a3735501af8610ce601a871f3e8cdcb5f41823624e045ffa1a673e58210b', 'GenomeCanadaAboutCrawler._curl': 'b03c568874f86562908f6ef2f6101da23f60010eb7c1aa1660737fd67a67cf00', 'GenomeCanadaAboutCrawler._pdf_to_text': 'b63344b4fc7df7e51eb151109f2c132694fbe96a10f9f802e95edf536411c017', 'GenomeCanadaAboutCrawler._parse_page': '2b3d9c0855b9ee1431cdc35f27beb17c7ef12a8db4e6ec5b00d6b8a10c726762', 'GenomeCanadaAboutCrawler.crawl': 'dbe25a99da88d29bb62da8b6559c5b54f1cd59dc48abcc0c418e731c3c027acf'}

if __name__ == '__main__':
    unittest.main()
