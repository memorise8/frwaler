"""kcg-go-kr-kcg: crawl() pages the board by POST (module ``_curl_post``); the
enumeration plan reads the same board by GET ``currPage=N``. The memory transport
serves both, so the unchanged crawl()/_process_item() output is compared with the
adapter output."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'kcg-go-kr-kcg'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []

        def get(url, *args, **kwargs):
            calls.append(url)
            return fixture['details'][url].encode('utf-8')

        def post(url, data, *args, **kwargs):
            calls.append(url)
            listing_calls.append(data['currPage'])
            return (fixture['listing_body'] if data['currPage'] == '1' else fixture['empty_body']).encode('utf-8')

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_get', get))
            stack.enter_context(patch.object(module, '_curl_post', post))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
