import copy
import unittest
from unittest.mock import patch

import tests.collector_split2_helpers as split2
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

_load = split2.load


def _load_with_post(site):
    # crawl() reads the listing with self._curl_post (POST currentPageNo=N); route it to
    # the memory transport the shared harness installs as _curl_get, crawl() unchanged.
    crawler, module = _load(site)
    crawler._curl_post = lambda url, data=None, **kwargs: crawler._curl_get(url)
    return crawler, module


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kinfa-or-kr-notificationpromotio'

    def native(self, fixture=None):
        with patch.object(split2, 'load', _load_with_post):
            return super().native(fixture)

    def test_detail_without_text_is_skipped_like_crawl(self):
        # The board-detail fallback normally lifts short content over 50 chars
        # (short_keeps_items); a detail page with no text at all is still skipped.
        f = copy.deepcopy(self.fixture)
        f['details'] = {u: '<html><body><div class="board-detail"></div></body></html>' for u in f['details']}
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kinfa-or-kr-notificationpromotio'
