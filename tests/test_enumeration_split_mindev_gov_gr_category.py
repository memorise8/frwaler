"""mindev-gov-gr-category: WP REST list (category 76) + REST detail -> paper,
compared with the unchanged crawl()."""
import unittest
from unittest.mock import patch

import tests.collector_split_helpers as base
from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'mindev-gov-gr-category'

    def native(self, fixture=None):
        # crawl() sleeps self.detail_delay (set by __init__, which the memory-only load skips).
        original_load = base.load

        def load(site):
            crawler, module = original_load(site)
            crawler.detail_delay = 0
            return crawler, module

        with patch.object(base, 'load', load):
            return super().native(fixture)


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'mindev-gov-gr-category'


if __name__ == '__main__':
    unittest.main()
