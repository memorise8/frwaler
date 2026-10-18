import copy
import unittest
from unittest.mock import patch

import tests.collector_split2_helpers as split2
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

_load = split2.load


def _load_with_curl(site):
    # crawl() fetches with self._curl(method, url, data) (listing by POST); route it to
    # the memory transport the shared harness installs as _curl_get, crawl() unchanged.
    crawler, module = _load(site)
    crawler._curl = lambda method, url, data=None, retries=3: crawler._curl_get(url)
    return crawler, module


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kipo-go-kr-ko'

    def native(self, fixture=None):
        with patch.object(split2, 'load', _load_with_curl):
            return super().native(fixture)

    def test_unparsable_detail_falls_back_to_list_row_like_crawl(self):
        # crawl() keeps list-row data when the detail page has no div.tbl_view, then
        # skips because "제목: ... / 작성일: ..." alone is under 50 characters.
        f = copy.deepcopy(self.fixture)
        f['details'] = {u: '<html><body><p>maintenance</p></body></html>' for u in f['details']}
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kipo-go-kr-ko'
