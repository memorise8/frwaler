import contextlib
import io
import unittest
from unittest.mock import patch

from crawler.incremental import Entry,observe_order,infer_order
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='afp-gov-au-search'
BODY='Controlled substantive source detail text for native persistence tests. '


class AfpSearchFailureTests(unittest.TestCase):
    def setUp(self):
        self.cls=CRAWLERS[SITE];self.c=self.cls(None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))
        self.item=dict(node_id='1001',title='Controlled source result',url='https://afp.gov.au/controlled',
                       listed_date='2026-10-01',datetime_raw='2026-10-01',category='Research',snippet=BODY*4)

    def test_missing_detail_never_falls_back_to_long_search_snippet(self):
        with patch.dict(self.cls.crawl.__globals__,{'_curl_get':lambda u:'list' if '/search?' in u else None,
                '_parse_list_page':lambda raw:[self.item]}), \
                patch.object(self.c,'_save_paper',side_effect=AssertionError('missing detail saved')):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_denial_is_not_a_source_item_or_empty_list(self):
        for where in ('list','detail'):
            with self.subTest(where=where):
                c=self.cls(None,delay=0);self.addCleanup(c._session.close)
                def fetch(url):return DENIAL if where=='list' or '/search?' not in url else 'list'
                with patch.dict(self.cls.crawl.__globals__,{'_curl_get':fetch,'_parse_list_page':lambda raw:[self.item]}), \
                        patch.object(c,'_save_paper',side_effect=AssertionError('denial saved')):
                    self.assertEqual(c.crawl(),0)
                self.assertEqual(c.delivery_source_failure,'authentication_failed')

    def test_unordered_numeric_IDs_do_not_override_unordered_dates(self):
        proof={};observe_order(proof,[Entry(k,{},d) for k,d in [('2','2026-01-02'),('1','2026-01-01'),('3','2026-01-03')]],True)
        self.assertEqual(infer_order(proof),('arbitrary','order_ambiguous'))

    def test_reviewed_search_hold_keeps_baseline_incomplete(self):
        r=eligibility(SITE,self.cls);self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('search_results_nonmonotonic',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
