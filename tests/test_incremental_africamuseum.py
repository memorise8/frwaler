import contextlib
import io
import unittest
from unittest.mock import patch

from crawler.incremental import Entry,observe_order,infer_order
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='africamuseum-be-en'


class AfricaMuseumIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_month_order_does_not_invent_a_day_or_numeric_source_ID(self):
        proof={};observe_order(proof,[Entry('news-'+str(i),{},d) for i,d in enumerate(['2026-10','2026-09','2026-09'])],False)
        self.assertEqual(infer_order(proof),('arbitrary','order_ambiguous'))
        self.assertEqual(self.c.crawl.__globals__['_parse_month_year']('September 2026'),'2026-09')

    def test_list_fetch_failure_is_not_successful_source_end(self):
        with patch.object(self.c,'_curl_get',return_value=None) as get:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed');self.assertFalse(self.c.delivery_exhausted)

    def test_detail_failure_does_not_skip_to_remaining_source_items(self):
        items=[('first','Title','2026-09','Long snippet '*20,self.c.base_url+'/en/research/news/first')]
        with patch.object(self.c,'_curl_get',side_effect=['listing',None]) as get, \
                patch.object(self.c,'_parse_list_page',return_value=items), \
                patch.object(self.c,'_save_paper',side_effect=AssertionError('failed detail saved')):
            self.assertEqual(self.c.crawl(),0);self.assertEqual(get.call_count,2)
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_recognized_denial_cannot_be_accepted_as_list_or_article(self):
        with patch.object(self.c,'_curl_get',return_value=DENIAL):self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_reviewed_month_precision_hold_keeps_baseline_incomplete(self):
        r=eligibility(SITE,type(self.c));self.assertEqual(r['classification'],'full_rescan_required')
        self.assertIn('month_precision',r['reason']);self.assertFalse(r['baseline_complete'])


if __name__=='__main__':unittest.main()
