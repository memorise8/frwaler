import unittest
from unittest.mock import patch

from tests import collector_split2_helpers as helpers
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

_load = helpers.load


def _bytes_load(site):
    """crawl() reads curl bytes; the shared memory transport hands out str bodies."""
    crawler, module = _load(site)
    for name in ('_parse_list_page', '_parse_detail_page'):
        original = getattr(module, name)
        setattr(module, name, lambda raw, *a, _f=original: _f(raw.encode('utf-8') if isinstance(raw, str) else raw, *a))
    return crawler, module


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'msit-go-kr-bbs'

    def native(self, fixture=None):
        with patch.object(helpers, 'load', _bytes_load):
            return super().native(fixture)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'msit-go-kr-bbs'
