import json
import unittest
from delivery.scripts.live_count_estimates import estimate_counts,scope_count,SOURCE,UNKNOWN,ARITHMETIC,SITEMAP


class CountEstimateTests(unittest.TestCase):
    def scope(self,total=213,url='https://source.example/list'):
        return dict(url=url,checks=dict(total_arithmetic=dict(status='verified',total=total,page_size=2),last_and_after=dict(status='verified')))

    def test_source_count_and_serializable_evidence(self):
        result=estimate_counts({'scopes':[self.scope()]})
        self.assertEqual(result['expected_items'],213)
        self.assertEqual(result['count_basis'],SOURCE)
        self.assertEqual(result['count_confidence'],'높음')
        json.dumps(result)

    def test_round_reported_count_suspects_cap(self):
        result=scope_count(self.scope(200))
        self.assertTrue(result['hidden_cap_suspected'])
        self.assertEqual(result['count_confidence'],'보통')

    def test_multiple_scopes_are_not_summed_or_deduplicated_by_count(self):
        result=estimate_counts({'scopes':[self.scope(url='https://source.example/a'),self.scope(url='https://source.example/b')]})
        self.assertIsNone(result['expected_items'])
        self.assertEqual(result['count_basis'],UNKNOWN)
        self.assertEqual(len(result['count_evidence']['scope_estimates']),2)

    def test_last_page_arithmetic_requires_observed_end(self):
        scope=self.scope();scope['checks']['total_arithmetic']=dict(status='not_reported',implied_total=29,page_count=3,page_size=10,last_count=9)
        self.assertEqual(scope_count(scope)['count_basis'],ARITHMETIC)
        scope['checks']['last_and_after']['status']='unconfirmed'
        self.assertIsNone(scope_count(scope)['expected_items'])

    def test_sitemap_index_count_is_not_item_count(self):
        scope=self.scope();scope['checks']['total_arithmetic']=dict(status='observed_XML_entry_count_not_document_total',totals=[dict(value=4)])
        scope['checks']['last_and_after']=dict(root='sitemapindex')
        self.assertIsNone(scope_count(scope)['expected_items'])
        scope['checks']['last_and_after']['root']='urlset'
        self.assertEqual(scope_count(scope)['count_basis'],SITEMAP)

    def test_recent_N_flag_survives_unknown_count(self):
        result=estimate_counts(dict(scopes=[],live_check='최근 100건 표시인지 확인'))
        self.assertTrue(result['hidden_cap_suspected'])
        self.assertIsNone(result['expected_items'])

    def test_selected_reviewed_endpoint_excludes_supplementary_landing(self):
        scope=self.scope();result=estimate_counts(dict(scopes=[scope,dict(url='https://source.example/',checks={})],strategy_review=dict(selected_scope=scope['url'])))
        self.assertEqual(result['expected_items'],213)
        json.dumps(result)


if __name__=='__main__':unittest.main()
