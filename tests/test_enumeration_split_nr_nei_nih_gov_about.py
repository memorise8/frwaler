"""NR-20261018: nei-nih-gov-about enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'nei-nih-gov-about'

    def test_short_abstract_skip_is_preserved(self):
        # PDF rows build their abstract from label + section intro (no fetch), so only the
        # sub-page row becomes short; both paths must drop exactly the same rows.
        import json
        from crawler.enumeration.adapters import clean_paper
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(1, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'nei-nih-gov-about'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
