"""NR2-20261018: lecese-fr-espace-presse enumeration split (crawl() unchanged), memory-only."""
import json
import unittest

from crawler.enumeration.adapters import clean_paper
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'lecese-fr-espace-presse'

    def test_short_abstract_skip_is_preserved(self):
        # crawl() appends the list subtitle to a body shorter than 100 characters before its
        # 50-character minimum; an empty body and no subtitle is the skip case.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        f['listing_body'] = f['listing_body'].replace('Sous-titre ', '')
        expected, _ = self.native(f)
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        f['listing_body'] = f['listing_body'].replace('field--name-field-sous-titre', 'x')
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'lecese-fr-espace-presse'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
