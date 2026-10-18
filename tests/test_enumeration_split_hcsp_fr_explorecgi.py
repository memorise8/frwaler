"""hcsp-fr-explorecgi: single-page list (no pager). crawl()'s _curl_get returns
(body, effective_url), so the native run uses a transport of that shape."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import load


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'hcsp-fr-explorecgi'

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; list_calls = []

        def get(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url], url
            list_calls.append(url)
            return fixture['listing_body'], url

        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(1, len(list_calls))
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'hcsp-fr-explorecgi'

    def test_ignored_page_parameter_ends_as_single_page(self):
        """The live source ignores ?page=N (page 2 == page 1): the runner must end at
        page 1 (wrap) with both records saved once."""
        from crawler.enumeration.http import Response
        from tests.collector_split_helpers import run_split
        same = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url)
        result, docs = run_split(self.SITE, end=same)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
