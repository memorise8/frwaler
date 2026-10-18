"""NR-20261018: openresearch-repository-anu-edu-au-search enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'openresearch-repository-anu-edu-au-search'

    def native(self, fixture=None):
        # crawl() reads its resume cursor and reports progress through BaseCrawler state.
        from tests import collector_split_helpers as h
        original = h.load

        def load(site):
            crawler, module = original(site)
            crawler.delivery_cursor = None
            crawler.detail_delay = 0
            crawler._advance_cursor = lambda *a, **k: None
            crawler._mark_exhausted = lambda: None
            return crawler, module
        import tests.nr_a_split_helpers as nh
        nh.load, saved = load, nh.load
        try:
            return super().native(fixture)
        finally:
            nh.load = saved


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'openresearch-repository-anu-edu-au-search'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
