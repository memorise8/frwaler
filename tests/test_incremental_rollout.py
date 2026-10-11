import unittest
from unittest.mock import patch
from delivery.scripts.incremental_rollout import build,accept


class Fake:
    def crawl(self):pass


class RolloutTests(unittest.TestCase):
    def test_unreviewed_adapter_missing_stays_pending_and_priority_is_preserved(self):
        with patch('delivery.scripts.incremental_rollout.CRAWLERS',{'a':Fake,'b':Fake,'c':Fake}):
            result=build({'sites':[{'site_id':'a','delivery':'True','reviewed_query_reported_total':5},
                                   {'site_id':'b','delivery':'True','reviewed_query_reported_total':100},
                                   {'site_id':'c','delivery':'False','reviewed_query_reported_total':1000}]})
        self.assertEqual([r['site_id'] for r in result['sites']],['b','a','c'])
        self.assertEqual(result['counts'],{'pending':3})

    def test_missing_tests_or_requests_cannot_close_a_batch(self):
        with self.assertRaises(ValueError):accept({'sites':[]},{'status':'passed','commit':'abc'})

    def test_different_source_cannot_be_accepted(self):
        review={'status':'passed','commit':'abc','unit_tests':'tests.log','isolated_request_measurement':'audit.json',
                'sites':[{'site_id':'a','status':'connected','reason':'adapter','source_sha256':'wrong'}]}
        with self.assertRaisesRegex(ValueError,'different source'):
            accept({'sites':[{'site_id':'a','source_sha256':'original'}]},review)

if __name__=='__main__':unittest.main()
