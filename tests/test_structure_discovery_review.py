import copy
import unittest

from delivery.scripts.discover_collection_structure import FEATURES
from delivery.scripts.review_structure_discovery import audit_site


class QualityReviewTests(unittest.TestCase):
    def result(self):
        item=dict(id='https://source.example/item/1',date='2020-01-01',date_basis='time_datetime')
        summary=dict(items=[item],date_order=dict(missing=0,increasing=0,decreasing=0),duplicate_id_count=0)
        features={k:{} for k in FEATURES}
        features.update(last_page=dict(verified=True,response=2),oldest_item=dict(item=item),
                        identity_stability=dict(same_ordered_IDs_on_repeat=True))
        return dict(site_id='example',status='measured',server='owned-server',finished_at='2026-10-12T00:00:00+00:00',
            requests=[dict(number=1,summary=summary),dict(number=2,summary=copy.deepcopy(summary))],
            scopes=[dict(url='https://source.example/list',status='measured',features=features,first_response=1)])

    def test_dated_first_page_does_not_prove_undated_last_item(self):
        result=self.result();result['requests'][1]['summary']['date_order']['missing']=5
        report=audit_site(result);self.assertEqual(report['audited_status'],'needs_review')
        self.assertIn('scope_0:last_page_contains_undated_items_oldest_date_not_established',report['issues'])

    def test_sitemap_lastmod_is_not_publication_evidence(self):
        result=self.result();result['scopes'][0]['features']['oldest_item']['item']['date_basis']='sitemap_lastmod_not_publication'
        self.assertIn('scope_0:oldest_date_role_unverified',audit_site(result)['issues'])

    def test_redirect_loop_requires_review_but_explicit_access_refusal_does_not(self):
        result=self.result();result['status']='unmeasurable';s=result['scopes'][0];s['status']='unmeasurable'
        s['blocked_reasons']=['redirect_scope_requires_review'];self.assertEqual(audit_site(result)['audited_status'],'needs_review')
        s['blocked_reasons']=['access_refused'];self.assertEqual(audit_site(result)['audited_status'],'unmeasurable')

    def test_all_eight_fields_and_request_budget_are_audited(self):
        result=self.result();del result['scopes'][0]['features']['date_filters'];result['request_budget']=1
        report=audit_site(result);self.assertIn('request_budget_exceeded',report['issues'])
        self.assertIn('scope_0:missing_required_features:date_filters',report['issues'])

    def test_audit_does_not_mutate_original_observations(self):
        result=self.result();before=copy.deepcopy(result);report=audit_site(result)
        self.assertEqual(result,before);self.assertEqual(report['audited_status'],'measured')


if __name__=='__main__':unittest.main()
