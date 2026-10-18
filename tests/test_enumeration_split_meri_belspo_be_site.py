"""L3b-20261018 (X3): enumeration split for meri-belspo-be-site.
crawl() reads one semicolon CSV (MERI_publications.csv), keeps the 'Research studies'
rows (carry-forward, first-wins de-duplication on pdf_url) and extracts each abstract
from the row's PDF with pdftotext -f 1 -l 2. The split lets the framework fetch the PDF
and run the same flags (adapter.pdf_text); several language editions share one
post_number RS-<n> (adapter.allow_shared_post_number, the registered collection's rule).
Synthetic CSV and PDFs only; pdftotext runs locally (no network)."""
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
from tests.test_enumeration_split_genomecanada_ca_about import make_pdf

SITE = 'meri-belspo-be-site'
CSV_URL = 'https://meri.belspo.be/site/docs/publications/MERI_publications.csv'
BASE = 'https://meri.belspo.be/site/docs/publications/'
W = 'E:\\I\\Publications\\3. Research studies\\'
CSV = ('\ufeffcategory;image;sub_category;description;reference;title 1;title 2;authors;year;download_NL;download_FR;download_EN\r\n'
       'Policy reports;p.jpg;BRISTI;Policy description long enough for the fallback rule here.;;BRISTI;Synthetic policy;A;2020;;;' + W + 'PR_1 EN.pdf\r\n'
       'Research studies;r.jpg;Science Connection;Research studies description long enough for fallback.;;No. 13;Synthetic careers;B. Author;2014;;;' + W + 'RS_13 EN.pdf\r\n'
       ';;;;;No. 9;Synthetic NL edition;C. Auteur;2009;' + W + 'RS_9 NL.pdf;;\r\n'
       ';;;;;No. 9;Synthetic FR edition;C. Auteur;2009;;' + W + 'RS_9 FR.pdf;\r\n'
       ';;;;;No. 9;Synthetic FR duplicate row;C. Auteur;2009;;' + W + 'RS_9 FR.pdf;\r\n'
       ';;;;;No. 8;Synthetic without file;D. Auteur;2008;;;\r\n')
LONG = ('Synthetic research text for a regression fixture describing a fictional survey of doctorate '
        'holders with enough words in each line to form one long paragraph for the abstract rule.')
PDFS = [BASE + 'RS_13%20EN.pdf', BASE + 'RS_9%20NL.pdf', BASE + 'RS_9%20FR.pdf']


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


def pdfs(text=LONG):
    return {u: make_pdf(text + ' ' + u[-10:-4]) for u in PDFS}


@unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext not installed')
class RegisteredSplit(unittest.TestCase):
    def native(self, files):
        crawler, _ = load(SITE)
        captured = []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = lambda url, retries=3, binary=False: files.get(url) if binary else CSV
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=None)
        self.assertEqual(len(captured), result)
        return captured

    def pipeline(self, files):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        parser = ListingParser(p.listing['parser'], adapter=adapter)
        items = parser.parse(Response(200, {'Content-Type': 'application/octet-stream'}, CSV.encode(),
                                      build_url(CSV_URL, {'page': 1}))).items
        bare = parser.parse(Response(200, {'Content-Type': 'application/octet-stream'}, CSV.encode(), CSV_URL)).items
        self.assertEqual([i.key for i in items], [i.key for i in bare])
        self.assertEqual(4, len(items))  # duplicate FR row dropped like crawl()

        def fetch(url):
            if url in files:
                return Response(200, {'Content-Type': 'application/pdf'}, files[url], url)
            return Response(200, {'Content-Type': 'text/html'}, b'<html><body>' + b'x' * 600 + b'</body></html>', url)
        papers = [adapter.materialize(item, SimpleNamespace(get=fetch)) for item in items]
        return [x for x in papers if x is not None]

    def test_full_paper_and_identity_equal_original_crawl(self):
        expected = self.native(pdfs())
        actual = self.pipeline(pdfs())
        self.assertEqual(4, len(expected))
        self.assertEqual([clean_paper(x) for x in expected], actual)
        self.assertEqual([paper_identity(x) for x in expected], [paper_identity(x) for x in actual])
        self.assertEqual(['RS-13', 'RS-9', 'RS-9', 'RS-8'], [x['external_id'] for x in actual])
        self.assertTrue(actual[0]['abstract'].startswith('Synthetic research text'))
        self.assertEqual('Research studies description long enough for fallback.', actual[3]['abstract'])

    def test_unreadable_pdf_falls_back_to_description_like_crawl(self):
        files = {u: b'%PDF-broken' + b'0' * 600 for u in PDFS}
        expected = self.native(files)
        self.assertEqual([clean_paper(x) for x in expected], self.pipeline(files))
        self.assertTrue(all(x['abstract'].startswith('Research studies description') for x in expected))

    def test_listing_without_research_rows_is_a_parse_error(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, b'<html>maintenance</html>', CSV_URL))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_original_code_unchanged(self):
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

    def test_end_to_end_shared_post_numbers_are_saved_for_review(self):
        files = pdfs()
        site = copy.deepcopy(plan()['sites'][SITE])
        site['request_interval_s'] = 0

        def handler(url):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            if url in files:
                return Response(200, {'Content-Type': 'application/pdf'}, files[url], url)
            if url.startswith(CSV_URL):
                return Response(200, {'Content-Type': 'application/octet-stream'}, CSV.encode(), url)
            return Response(200, {'Content-Type': 'text/html'}, b'<html>publications page</html>', url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertIn('shared_post_number_rows', result['stop_reason'])
        self.assertEqual(['RS-13', 'RS-8', 'RS-9', 'RS-9'], sorted(d['post_number'] for d in docs))


ORIGINAL_SHA256 = {"MeriBelspoBeSiteCrawler._curl_get": "f381d29112de6aba2ad28485a6e9e32b3729680020d2331f46d9e7c4da7822cd", "MeriBelspoBeSiteCrawler._pdf_abstract": "f72e2d5d13d311baa53bd81e8b7eb331d0a188f38c59e192e0a3b56d488c4d2d", "MeriBelspoBeSiteCrawler._best_paragraph": "85d292ef18c475a798e2ec2a4041a0b527777f9395eb807de1f297726c4dcbd9", "MeriBelspoBeSiteCrawler._win_path_to_url": "3c11af7d4def98d7c37d277c27b7897769a7aa705d8b6a4b9261980e74f407f4", "MeriBelspoBeSiteCrawler._extract_number": "f7bc80d597b72ee6c7368ffb5aab971089c23efeb87b05a52b220750e93f8d4f", "MeriBelspoBeSiteCrawler.crawl": "419be8a98bd34bf20cb00a193c5a9316c09f9ee7f5d961c10739d4980248a3ea"}

if __name__ == '__main__':
    unittest.main()
