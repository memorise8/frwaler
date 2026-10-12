import unittest
from tests.collector_split_helpers import SplitChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'minfin-gov-gr-grafeio-typou'


class PageBinding(unittest.TestCase):
    def test_page_binding_survives_the_strategy_start_checks(self):
        # The list method reads ?page= from the listing URL; the page-less first-page
        # comparison of last_page_backward has no such parameter.
        from tests.collector_split_helpers import run_split, split_plan
        self.assertIs(False, split_plan(RegisteredSplit.SITE)['last_page'].get('check_unparameterized_first_page'))
        result, docs = run_split(RegisteredSplit.SITE, total=2)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))
