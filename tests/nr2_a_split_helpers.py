"""NR2-20261018 group A: shared checks for enumeration splits (crawl() unchanged).
Extends tests.nr_a_split_helpers for list methods that take the raw response bytes."""
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks  # noqa: F401


class NR2SplitChecks(NRSplitChecks):
    def test_added_listing_rejects_wrong_shape(self):
        p, _ = self._parser()
        c, _ = load(self.SITE)
        bad = {'error': 'not a listing'}
        args = {'html': bad, 'raw': bad, 'json': bad, 'soup': bad, 'url': p.listing['url']}
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                c._enumeration_items(*[args[t] for t in p.section('adapter').get('list_args', ['html'])])
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)
