import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kipf-re-kr-bbs'

    def native(self, fixture=None):
        # This crawler's transport returns bytes (curl stdout); serve the fixture as bytes.
        import contextlib, copy, io, json
        from unittest.mock import patch
        from tests.collector_split_helpers import load
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url].encode('utf-8')
            listing_calls.append(url)
            return (fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']).encode('utf-8')
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_get', transport))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kipf-re-kr-bbs'
