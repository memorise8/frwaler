"""NR2-20261018: molit-go-kr-usr enumeration split (crawl() unchanged), memory-only.
The plan's second step fetches the first DWN.jsp PDF link of the detail page; the paper method
gets both pages (X2 first_html) and runs crawl()'s in-process pdfminer helper on the PDF bytes."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper
from tests.collector_split_helpers import load
from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks

SITE = 'molit-go-kr-usr'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        captured, list_calls = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._session = SimpleNamespace(verify=True)
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def request(url, params=None, **kw):
            list_calls.append(params)
            body = fixture['listing_body'] if len(list_calls) == 1 else fixture['empty_body']
            return SimpleNamespace(content=body.encode('utf-8'))

        def fetch(session, url, **kw):
            value = fixture['details'][url]
            return SimpleNamespace(content=value.encode('utf-8'))
        with patch.object(crawler, '_request', request), patch.object(module, '_fetch_with_retry', fetch), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, []

    def test_short_abstract_skip_is_preserved(self):
        # crawl() joins title, og:title and bullets; the split keeps the same join and minimum.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'x'))
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def test_pdf_text_is_used_like_crawl(self):
        crawler, module = load(SITE)
        item = crawler._enumeration_items(self.fixture['listing_body'].encode(), '')[0]
        detail = self.fixture['details'][item['url']]
        pdf = b'%PDF-1.4 ' + b'0' * 300
        with patch.object(module, '_extract_pdf_text', lambda b: 'P' * 1000 if b == pdf else ''):
            paper = crawler._enumeration_paper(detail, pdf, item)
        self.assertEqual('P' * 800, paper['abstract'])


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
