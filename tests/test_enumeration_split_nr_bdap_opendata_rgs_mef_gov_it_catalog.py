"""NR-20261018: bdap-opendata-rgs-mef-gov-it-catalog enumeration split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = 'bdap-opendata-rgs-mef-gov-it-catalog'

    def test_added_listing_rejects_wrong_shape(self):
        # list_args is ["raw"] (bytes decoded like crawl()'s _curl); anything else is refused.
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        from tests.collector_split_helpers import load
        c, _ = load(self.SITE)
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                c._enumeration_items({'error': 'not a listing'})
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = 'bdap-opendata-rgs-mef-gov-it-catalog'
    END_STATUS = 200


if __name__ == '__main__':
    unittest.main()
