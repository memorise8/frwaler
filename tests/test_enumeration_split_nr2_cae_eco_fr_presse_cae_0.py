"""NR2-20261018: cae-eco-fr-presse-cae-0 enumeration split (crawl() unchanged), memory-only.
List pages /Presse-CAE-N (X1 path paging, from 0)."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = 'cae-eco-fr-presse-cae-0'

    def test_short_abstract_skip_is_preserved(self):
        # crawl() keeps a PDF-only entry (empty summary) and fails only entries without
        # summary and PDF; the split keeps the same rule.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], ''))
        expected, _ = self.native(f)
        self.assertEqual(1, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'cae-eco-fr-presse-cae-0'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
