"""koti-re-kr-user: the unchanged crawl()/_fetch_and_save() output (module
``_curl_get`` served from memory) is compared with the enumeration adapter output
(_enumeration_items over ``pg=N`` board pages, _enumeration_paper over detail HTML)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'koti-re-kr-user'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []

        def get(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_get', get))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_popular_box_is_not_a_board_row(self):
        crawler, _ = load(self.SITE)
        box = ('<div class="popularity_box"><a href="./bassRsrchReprtView.do?bbs_no=999&pg=1">x</a></div>')
        html = self.fixture['listing_body'].replace('<div class="list_detail_body">',
                                                    '<div class="list_detail_body">' + box)
        self.assertEqual(['101', '102'], [r['bbs_no'] for r in crawler._enumeration_items(html)])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
