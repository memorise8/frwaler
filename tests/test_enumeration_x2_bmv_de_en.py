"""L3b X2 (ext:pdf_text): bmv-de-en — list cards (?gtp=81202_liste%3D{page}, first page without it),
each card's PDF is the detail request and the framework runs pdftotext with crawl()'s arguments
(-l 10); the method applies _extract_pdf_text()'s checks (%PDF, over 500 bytes) and clean-up, so
the paper equals crawl()'s. Past the last page the site renders unfilled '$doc.id_' cards.
crawl() is unchanged. Synthetic HTML/PDF only, no network (pdftotext runs locally)."""
import contextlib
import copy
import io
import json
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.pdftext import extract
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load
from tests.test_enumeration_extensions_x2 import make_pdf

SITE = 'bmv-de-en'
ROOT = Path(__file__).resolve().parents[1]
PLAN = json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.x2.json')).read_text())['sites'][SITE]
LIST = 'https://www.bmv.de/EN/Services/Publications/publications.html'
TEXT = 'Synthetic publication text for the offline enumeration test ' * 6
EMPTY = ('<html><body><div class="container-listview"><ul class="card-list">'
         + '<li class="card card-list-item $doc.id_"><p class="card-date">$doc.date</p></li>' * 3
         + '</ul></div></body></html>')


def card(n, pdf=True):
    btn = (f'<a class="card-btn" href="/SharedDocs/EN/publications/doc-{n}.pdf?__blob=publicationFile">PDF</a>'
           if pdf else '')
    return (f'<li class="card card-list-item {n}"><p class="card-topline"><span class="sr-only">Type</span>Brochure</p>'
            f'<a class="card-link" href="SharedDocs/EN/publications/doc-{n}.html"><strong>Doc {n}</strong></a>'
            f'<p class="card-date">2025.0{n % 9 + 1}.10</p><p class="card-format"><span class="sr-only">Topic</span>Mobility</p>'
            f'{btn}</li>')


def listing(cards):
    return f'<html><body><div class="container-listview"><ul class="card-list">{"".join(cards)}</ul></div></body></html>'


PDFS = {f'https://www.bmv.de/SharedDocs/EN/publications/doc-{n}.pdf?__blob=publicationFile': make_pdf(TEXT + str(n))
        for n in (101001, 101002)}
PAGE1 = listing([card(101001), card(101002), card(101003, pdf=False)])


@unittest.skipUnless(shutil.which('pdftotext'), 'pdftotext not installed')
class X2PdfText(unittest.TestCase):
    def setUp(self):
        self.denials = []

        def denied(*args, **kwargs):
            self.denials.append('io')
            raise AssertionError('attempted network I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect'):
            p = patch(name, denied)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))
        self.assertTrue(all(len(b) > 500 for b in PDFS.values()))

    def native(self):
        crawler, _ = load(SITE)
        crawler._delay = 0
        crawler._last_save_created = True
        saved = []
        crawler._save_paper = lambda paper: saved.append(copy.deepcopy(paper))
        crawler._curl_get = lambda url: PAGE1 if url == LIST else EMPTY
        crawler._curl_pdf = lambda url: PDFS.get(url)
        with contextlib.redirect_stdout(io.StringIO()), patch('time.sleep'):
            crawler.crawl()
        return saved

    def test_paper_equals_crawl(self):
        expected = self.native()
        crawler, _ = load(SITE)
        spec = PLAN['adapter']['pdf_text']
        got = []
        for row in crawler._enumeration_items(PAGE1, LIST):
            raw = PDFS.get(row['fetch_url'], b'<html>detail page</html>')
            paper = crawler._enumeration_paper(extract(raw, spec), raw, row)
            if paper is not None:
                got.append(paper)
        self.assertEqual(expected, got)
        self.assertEqual(['101001', '101002'], [p['external_id'] for p in got])
        self.assertEqual([], crawler._enumeration_items(EMPTY, LIST + '?gtp=81202_liste%3D5'))
        row = crawler._enumeration_items(PAGE1, LIST)[0]
        self.assertIsNone(crawler._enumeration_paper(None, PDFS[row['fetch_url']], row))  # pdftotext failed
        self.assertIsNone(crawler._enumeration_paper('x' * 80, b'<html>' + b' ' * 600, row))  # not a PDF

    def test_plan_end_to_end(self):
        from tests.enumeration_fakes import Env
        site_plan({'version': 1, 'sites': {SITE: PLAN}}, SITE, registered=[SITE])
        plan = dict(PLAN, request_interval_s=0)
        urls = []

        def handler(url):
            urls.append(url)
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in PDFS:
                return Response(200, {'Content-Type': 'application/pdf'}, PDFS[url], url)
            if parts.path.endswith('.html') and parts.path.startswith('/SharedDocs/'):
                return Response(200, {'Content-Type': 'text/html'}, '<html>detail</html>', url)
            if parts.path == urlsplit(LIST).path:
                return Response(200, {'Content-Type': 'text/html'}, PAGE1 if not parts.query else EMPTY, url)
            return Response(404, {}, b'')
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            docs = env.docs()
        finally:
            env.close()
        self.assertIn(result['status'], ('complete', 'complete_with_review'), result)
        self.assertEqual(['101001', '101002'], sorted(d['post_number'] for d in docs))
        self.assertTrue(any('gtp=81202_liste' in u for u in urls))


if __name__ == '__main__':
    unittest.main()
