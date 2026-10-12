import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'brgm-fr-fr'

    def test_cards_outside_main_match_original_crawl(self):
        import copy
        fixture = copy.deepcopy(self.fixture)
        fixture['listing_body'] = fixture['listing_body'].replace('<main>', '').replace('</main>', '')
        expected, _ = self.native(fixture)
        self.assertEqual(2, len(expected))
        self.assertEqual(expected, self.pipeline(fixture))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'brgm-fr-fr'
