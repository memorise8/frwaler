import unittest
from unittest.mock import patch

import tests.collector_split_helpers as base
from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'ungeneva-org-en'

    def native(self, fixture=None):
        # crawl() branches on delivery_mode, which BaseCrawler.__init__ (skipped here) sets.
        original_load = base.load

        def load(site):
            crawler, module = original_load(site)
            crawler.delivery_mode = 'incremental'
            crawler._report_source_failure = lambda *a, **k: None
            return crawler, module

        with patch.object(base, 'load', load):
            return super().native(fixture)


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'ungeneva-org-en'
