import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kipf-re-kr-kor'

    def native(self, fixture=None):
        # crawl() fetches through self._curl_fetch and reads the detail with POST
        # (serialNo in the body); map that POST to the fixture page of the item URL.
        import contextlib, copy, io
        from unittest.mock import patch
        from tests.collector_split_helpers import load
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        view = 'https://www.kipf.re.kr/kor/Publication/CenterPeriodicals/kiPublish/CB/Center/view.do'
        def fetch(url, method='GET', data=None, retries=3):
            if method == 'POST':
                url = f"{view}?serialNo={data['serialNo']}"
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_fetch = fetch
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kipf-re-kr-kor'
