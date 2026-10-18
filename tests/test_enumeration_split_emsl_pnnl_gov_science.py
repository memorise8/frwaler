"""emsl-pnnl-gov-science split: listing rows + PDF response headers (instead of crawl's curl HEAD).
Memory only; crawl() runs with patched module transports."""
import ast
import contextlib
import copy
import hashlib
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import ROOT, SplitChecks, SplitRunChecks, load

SITE = 'emsl-pnnl-gov-science'
HEAD = 'HTTP/1.1 200 OK\ncontent-type: text/html'   # what the pipeline's fake PDF response carries


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured, calls, list_calls = [], [], []

        def get_html(url):
            calls.append(url); list_calls.append(url)
            return fixture['listing_body'] if len(list_calls) == 1 else '<html><main/></html>'

        def head(url):
            calls.append(url)
            self.assertIn(url, fixture['details'])
            return HEAD
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_get_html', get_html))
            stack.enter_context(patch.object(module, '_curl_head', head))
            stack.enter_context(patch.object(module, '_curl_post_ajax', lambda *a, **k: None))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def test_short_abstract_skip_is_preserved(self):
        # Short teasers are replaced by crawl's synthesized fallback abstract (never skipped).
        f = copy.deepcopy(self.fixture)
        f['listing_body'] = f['listing_body'].replace(self.fixture['long_text'], 'Short.')
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_all_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (self.SITE + '.py')).read_text()
        tree = ast.parse(code)
        nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        nodes += [n for c in tree.body if isinstance(c, ast.ClassDef) for n in c.body if isinstance(n, ast.FunctionDef)]
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest() for n in nodes}
        for name, digest in self.fixture['original_function_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_last_modified_header_feeds_listed_date_like_head(self):
        crawler, module = load(self.SITE)
        from crawler.enumeration.http import Response
        url = next(iter(self.fixture['details']))
        resp = Response(200, {'Last-Modified': 'Wed, 15 Apr 2026 10:00:00 GMT',
                              'Content-Disposition': 'attachment; filename="named.pdf"'}, b'%PDF', url)
        meta = crawler._enumeration_detail(resp, {'pdf_url': url})
        head = 'HTTP/1.1 200 OK\nLast-Modified: Wed, 15 Apr 2026 10:00:00 GMT\nContent-Disposition: attachment; filename="named.pdf"'
        with patch.object(module, '_curl_head', lambda u: head):
            self.assertEqual(module._fetch_pdf_metadata(url), meta)
        self.assertEqual('2026-04-15', meta['listed_date'])

    def test_rendered_cards_that_do_not_parse_are_not_the_end(self):
        crawler, _ = load(self.SITE)
        changed = self.fixture['listing_body'].replace('<h3>', '<h4>').replace('</h3>', '</h4>')
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(changed, 'https://www.emsl.pnnl.gov/science/reports?page=0')


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', '<html><body><main></main></body></html>')})

    def test_short_paper_does_not_silently_become_complete(self):
        # Short teasers get crawl's synthesized fallback abstract, so both rows are still saved.
        from tests.collector_split_helpers import run_split
        from crawler.enumeration.http import Response
        fixture = copy.deepcopy(self.fixture)
        fixture['listing_body'] = fixture['listing_body'].replace(self.fixture['long_text'], 'Short.')
        status, ctype, body = self.END_PAGES[SITE]
        result, docs = run_split(SITE, fixture=fixture, end=lambda url: Response(status, {'Content-Type': ctype}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
