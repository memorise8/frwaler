import unittest
from urllib.parse import parse_qs, urlsplit

from delivery.health.listing_probe import probe, list_url
from delivery.health.source_observer import HostGate


class ListingProbeTest(unittest.TestCase):
    def config(self):
        return {'base_url': 'https://example.org', 'list_page': {
            'url': 'https://example.org/list', 'params': {'board': '23'},
            'pagination': {'type': 'query_param', 'param': 'page', 'start': 0, 'step': 1},
            'selectors': {'item_container': 'article', 'item_link': 'a[href]'}}}

    def fetch(self, url, **kw):
        page = parse_qs(urlsplit(url).query)['page'][0]
        body = f'<article><a href="/document/{page}">Title</a></article>'.encode()
        return {'status': 200, 'curl_exit': 0, 'headers': {}, 'body_complete': True, 'body': body}

    def test_page_zero_and_scope_parameters_are_preserved(self):
        query = parse_qs(urlsplit(list_url(self.config(), 0)).query)
        self.assertEqual(query, {'board': ['23'], 'page': ['0']})

    def test_distinct_pages_do_not_certify_full_coverage_or_save(self):
        r = probe({'site_id': 'fixture'}, self.config(), HostGate(0), fetch=self.fetch)
        self.assertEqual(r['state'], 'distinct_next_page_links_observed_not_full_coverage')
        self.assertEqual(r['transition']['next_only'], 1)
        self.assertIsNone(r['source_total'])
        self.assertEqual(r['persistence'], 'not_tested')

    def test_ignored_page_parameter_is_detected(self):
        r = probe({'site_id': 'fixture'}, self.config(), HostGate(0),
                  fetch=lambda *a, **kw: self.fetch('https://example.org/list?page=0'))
        self.assertEqual(r['state'], 'same_links_on_next_page_requires_review')

    def test_missing_list_configuration_never_fetches_homepage(self):
        r = probe({'site_id': 'fixture'}, {'crawl_type': 'single-page'}, HostGate(0),
                  fetch=lambda *a, **kw: self.fail('unexpected request'))
        self.assertEqual(r['state'], 'missing_list_page_configuration')

    def test_empty_first_page_is_not_confirmed_zero_total(self):
        def fetch(*a, **kw):
            r = self.fetch('https://example.org/list?page=0');r['body'] = b'<html></html>';return r
        r = probe({'site_id': 'fixture'}, self.config(), HostGate(0), fetch=fetch)
        self.assertEqual(r['state'], 'no_list_links_first_page')
        self.assertIsNone(r['source_total'])

    def test_bad_selector_reports_error_without_exposing_raw_response(self):
        c = self.config();c['list_page']['selectors']['item_container'] = '#18241'
        r = probe({'site_id': 'fixture'}, c, HostGate(0), fetch=self.fetch)
        self.assertEqual(r['exception_type'], 'SelectorSyntaxError')
        self.assertEqual(r['state'], 'configuration_or_selector_or_transport_error')


if __name__ == '__main__':
    unittest.main()
