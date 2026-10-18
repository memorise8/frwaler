"""kamco-or-kr-portal: crawl() paginates by POST (``_curl_post``); the enumeration
plan reads the same board pages by GET ``page=N``. The memory transport below
serves both, so the unchanged crawl() output is compared with the adapter output."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'kamco-or-kr-portal'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        crawler._curl_post = lambda url, body, *a, **k: transport(url)
        crawler._session = SimpleNamespace(get=lambda *a, **k: (_ for _ in ()).throw(AssertionError('session')))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_error_detail_page_is_skipped_like_crawl(self):
        f = copy.deepcopy(self.fixture)
        for url in f['details']:
            f['details'][url] = '<html><script>alert("없는 게시물");</script></html>'
        self.assertEqual([], self.native(f)[0])
        self.assertEqual([], self.pipeline(f))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
