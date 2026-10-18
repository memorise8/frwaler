"""NR2-20261018: dst-dk-da enumeration split (crawl() unchanged), memory-only."""
import json
import unittest

from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = 'dst-dk-da'

    def test_short_abstract_skip_is_preserved(self):
        # crawl() falls back to og:description when no paragraph qualifies; both short -> skip.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'dst-dk-da'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
