import contextlib
import io
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility
from tests.test_incremental_accc import DENIAL

SITE='arn-se-om-arn'


def listing(hrefs):
    return '<html><body><main>'+''.join(f'<a href="{href}">Annual report</a>' for href in hrefs)+'</main></body></html>'


class ARNIncrementalTests(unittest.TestCase):
    def setUp(self):
        self.c=CRAWLERS[SITE](None,delay=0);self.addCleanup(self.c._session.close)
        self.enterContext(contextlib.redirect_stdout(io.StringIO()));self.enterContext(patch('time.sleep'))

    def test_partial_or_HTTP_error_body_is_rejected(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=22,stdout=listing(['/files/2025.pdf']).encode())) as run:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(run.call_count,3)
            self.assertIn('-fsk',run.call_args.args[0])
        self.assertEqual(self.c.delivery_source_failure,'fetch_failed')

    def test_access_challenge_is_not_healthy_empty_list(self):
        with patch.object(self.c.crawl.__globals__['subprocess'],'run',return_value=SimpleNamespace(returncode=0,stdout=DENIAL.encode())) as run:
            self.assertEqual(self.c.crawl(),0);self.assertEqual(run.call_count,1)
        self.assertEqual(self.c.delivery_source_failure,'authentication_failed')

    def test_empty_structure_has_no_exhaustion_proof(self):
        with patch.object(self.c,'_curl_get',return_value=listing([])):
            self.assertEqual(self.c.crawl(),0)
        self.assertEqual(self.c.delivery_source_failure,'invalid_response');self.assertFalse(self.c.delivery_exhausted)

    def test_unknown_reporting_ID_is_not_saved_under_unknown(self):
        with patch.object(self.c,'_curl_get',return_value=listing(['/files/annual.pdf'])),patch.object(self.c,'_save_paper') as save:
            self.assertEqual(self.c.crawl(),0);save.assert_not_called()
        self.assertEqual(self.c.delivery_source_failure,'invalid_response')

    def test_two_different_files_for_same_year_are_reported_as_collision(self):
        papers=[]
        def save(p):papers.append(p);self.c._last_save_created=True
        with patch.object(self.c,'_curl_get',return_value=listing(['/files/2025.pdf','/files/2025-revised.pdf'])),patch.object(self.c,'_save_paper',save):
            self.assertEqual(self.c.crawl(),1)
        self.assertEqual(len(papers),1);self.assertEqual(self.c.delivery_source_failure,'invalid_response')
        self.assertIsNone(papers[0]['published_date']);self.assertIsNone(papers[0]['listed_date'])

    def test_repeated_identical_file_is_not_a_year_collision(self):
        def save(p):self.c._last_save_created=True
        with patch.object(self.c,'_curl_get',return_value=listing(['/files/2025.pdf','/files/2025.pdf'])),patch.object(self.c,'_save_paper',save):
            self.assertEqual(self.c.crawl(),1)
        self.assertIsNone(self.c.delivery_source_failure)

    def test_hold_preserves_reporting_period_semantics(self):
        result=eligibility(SITE,type(self.c));self.assertEqual(result['classification'],'full_rescan_required')
        self.assertIn('reporting_period_IDs_not_arrival_chronology',result['reason']);self.assertFalse(result['baseline_complete'])


if __name__=='__main__':unittest.main()
