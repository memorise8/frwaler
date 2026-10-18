import unittest
from unittest.mock import patch

import tests.collector_split2_helpers as split2
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

_load = split2.load


def _load_with_curl(site):
    # This crawler fetches with self._curl (not _curl_get); route it to the memory
    # transport the shared harness installs as _curl_get, so crawl() itself is unchanged.
    crawler, module = _load(site)
    crawler._curl = lambda url, **kwargs: crawler._curl_get(url)
    return crawler, module


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'khs-go-kr-cop'

    def native(self, fixture=None):
        with patch.object(split2, 'load', _load_with_curl):
            return super().native(fixture)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'khs-go-kr-cop'
