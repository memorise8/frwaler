import unittest

from delivery.scripts.route_structure_discovery import source_hints, security_hold


class RoutingTests(unittest.TestCase):
    def test_literal_endpoint_and_nested_scope_are_found_without_execution(self):
        source='''
import something_unavailable
BASE_URL = "https://source.example"
_WP_ENDPOINT = BASE_URL + "/wp-json/wp/v2/publications"
_SCOPES = {"press": {"entry":"/press/"}, "reports": {"entry":"/reports/"}}
raise RuntimeError("this module must never execute")
'''
        result=source_hints(source,'https://source.example')
        urls={r['url'] for r in result['endpoints']}
        self.assertEqual(urls,{'https://source.example/wp-json/wp/v2/publications','https://source.example/press/','https://source.example/reports/'})
        self.assertFalse(result['measured'])

    def test_sensitive_and_runtime_endpoints_are_not_exposed(self):
        source='''
API_URL = "https://source.example/api?api_key=secret"
SEARCH_URL = make_authenticated_url()
'''
        result=source_hints(source,'https://source.example')
        self.assertEqual(result['endpoints'],[]);self.assertTrue(result['unresolved_constants'])

    def test_listing_selectors_are_hints_not_detail_execution(self):
        source='''
def _parse_listing(html):
    return soup.select("div.record.publication")
def _parse_detail(html):
    return soup.select("div.body")
'''
        result=source_hints(source,'https://source.example')
        self.assertEqual([x['selector'] for x in result['listing_selector_hints']],['div.record.publication'])

    def test_observed_hold_blocks_routes_even_to_an_alternative_API_host(self):
        result={'scopes':[{'url':'https://source.example/list','blocked_reasons':['access_refused']}]}
        self.assertTrue(security_hold(result,{}))
        result['scopes'][0]['blocked_reasons']=[]
        self.assertTrue(security_hold(result,{'source.example':{'reason':'blocked'}}))
        self.assertFalse(security_hold(result,{}))


if __name__=='__main__':unittest.main()
