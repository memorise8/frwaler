import unittest
from delivery.scripts.finalize_live_batch import disposition
from delivery.scripts.live_check_contracts import CHECKS
from delivery.scripts.live_count_estimates import UNKNOWN


class BatchDispositionTests(unittest.TestCase):
    def sample(self):
        url='https://source.example/list'
        plan=dict(site_id='x',delivery=True,kind='페이지 파라미터',live_check='Check page 3',
                  targets=[[dict(url=url+'?page=3',provenance='explicit_live_check_URL')]],unresolved_URLs=[])
        scope=dict(url=url,status='unconfirmable_custom',strategy='불가',confidence='낮음',
                   limitations=['listing_selector_contract_not_established'],
                   checks={k:dict(status='unconfirmed',reason='listing_selector_contract_not_established') for k in CHECKS})
        result=dict(site_id='x',live_check=plan['live_check'],status='unconfirmable_custom',strategy='불가',confidence='낮음',
                    request_budget=30,requests=[dict(url=url,status=200,number=1)],scopes=[scope],
                    actual_HTTP_responses=1,database_connections=0,documents_saved=0,
                    expected_items=None,count_basis=UNKNOWN,count_confidence='낮음',hidden_cap_suspected=False)
        return plan,result

    def test_custom_parser_reason_does_not_excuse_unrequested_concrete_URL(self):
        plan,result=self.sample();audit=disposition(plan,result)
        self.assertTrue(any(i.startswith('unresolved_actionable_URL:') for i in audit['issues']))

    def test_request_budget_is_an_explicit_terminal_disposition(self):
        plan,result=self.sample()
        result['requests']*=30
        result['requests']=[{**r,'number':n} for n,r in enumerate(result['requests'],1)]
        result['actual_HTTP_responses']=30
        audit=disposition(plan,result)
        self.assertFalse(audit['issues'])
        self.assertEqual(audit['URL_dispositions'][0]['disposition'],'bounded_request_budget_exhausted')
        self.assertEqual(audit['condition_dispositions'][0]['status'],'unconfirmed_under_allowed_inability_disposition')

    def test_strategy_promotion_requires_review(self):
        plan,result=self.sample();result['status']='confirmed'
        self.assertIn('confirmed_strategy_without_semantic_review',disposition(plan,result)['issues'])

    def test_no_live_or_prior_source_evidence_is_not_completion(self):
        plan,result=self.sample();result.update(requests=[],scopes=[],actual_HTTP_responses=0)
        self.assertIn('no_live_or_inherited_evidence',disposition(plan,result)['issues'])


if __name__=='__main__':unittest.main()
