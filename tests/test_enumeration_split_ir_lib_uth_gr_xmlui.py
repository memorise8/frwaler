"""L4-20261018: enumeration split for ir-lib-uth-gr-xmlui (DSpace 6 discover, rpp=50, page=1..).
crawl() fetches https://ir.lib.uth.gr (port 443 no longer speaks TLS); the plan fetches
http:// while the saved url keeps the registered https form. The fixture's detail pages
are keyed by the https URL crawl() requests; the plan path is served the same bodies
under the http URL it requests."""
import copy
import unittest

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, split_fixture

SITE = 'ir-lib-uth-gr-xmlui'
EMPTY = '<html><body><div id="aspect_discovery_SimpleSearch_div_search"></div></body></html>'


def http_keyed(fixture):
    fx = copy.deepcopy(fixture)
    fx['details'] = {k.replace('https://', 'http://', 1): v for k, v in fx['details'].items()}
    return fx


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def pipeline(self, fixture=None, redirect=False):
        return super().pipeline(http_keyed(fixture or self.fixture), redirect=redirect)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?rpp=50&page=19'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_identity_keeps_https_url_and_handle(self):
        expected, _ = self.native()
        self.assertEqual(['11615/5001', '11615/5000'], [p['external_id'] for p in expected])
        actual = self.pipeline()
        self.assertTrue(all(p['url'].startswith('https://ir.lib.uth.gr/xmlui/handle/') for p in self.raw_papers))
        self.assertEqual(2, len(actual))

    def test_paper_method_leaves_no_instance_override(self):
        crawler, _ = load(self.SITE)
        crawler._enumeration_paper(next(iter(self.fixture['details'].values())),
                                   {'handle_path': '11615/5001', 'url': 'http://ir.lib.uth.gr/xmlui/handle/11615/5001'})
        self.assertNotIn('_curl_get', vars(crawler))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        from crawler.enumeration.http import Response
        end = lambda url: Response(200, {'Content-Type': 'text/html'}, EMPTY, url)
        result, docs = run_split(self.SITE, end=end, fixture=http_keyed(split_fixture(self.SITE)))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'https://ir.lib.uth.gr/xmlui/handle/11615/5001',
                          'https://ir.lib.uth.gr/xmlui/handle/11615/5000'}, {d['meta_url'] for d in docs})


if __name__ == '__main__':
    unittest.main()
