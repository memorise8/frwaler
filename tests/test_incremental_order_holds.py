import unittest
from unittest.mock import patch
from crawler.sites import CRAWLERS
from delivery.incremental_status import eligibility,reviewed_hold


class OrderHoldTests(unittest.TestCase):
    def test_reviewed_holds_preserve_evidence_and_never_certify_baseline(self):
        for site in ('amolf-nl-publications','krivet-re-kr-kor'):
            with self.subTest(site=site):
                result=eligibility(site,CRAWLERS[site])
                self.assertEqual(result['classification'],'full_rescan_required')
                self.assertFalse(result['baseline_complete'])
                self.assertIsNone(result['adapter'])
                self.assertIn('reason',result['order_review'])
                self.assertEqual(len(result['order_review']['evidence_sha256']),64)
                self.assertTrue(result['periodic_full_check'])

    def test_changed_native_code_invalidates_prior_hold_evidence(self):
        class Changed:
            def crawl(self):pass
        self.assertIsNone(reviewed_hold('amolf-nl-publications',Changed))
        self.assertNotIn('order_review',eligibility('amolf-nl-publications',Changed))

    def test_future_adapter_takes_precedence_over_prior_native_order_hold(self):
        class Implemented:pass
        with patch.dict('delivery.incremental_registry.ADDITIONAL_ADAPTERS',{'amolf-nl-publications':Implemented}):
            result=eligibility('amolf-nl-publications',CRAWLERS['amolf-nl-publications'])
        self.assertEqual(result['classification'],'pending_initial_exhaustion')
        self.assertNotIn('order_review',result)

if __name__=='__main__':unittest.main()
