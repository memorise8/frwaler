import contextlib
import io
import unittest
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.health.access_challenge import access_challenge

DENIAL='''<html><head><title>Access Denied</title></head><body><h1>Access Denied</h1>
You don't have permission to access "http://www.accc.gov.au/search?" on this server.
<p>Reference #TEST-ONLY</p><p>https://errors.edgesuite.net/TEST-ONLY</p></body></html>'''
SITES=('accc-gov-au-about-us','accc-gov-au-search')


class AcccAccessTests(unittest.TestCase):
    def test_actual_edge_shape_classified_without_returning_reference(self):
        self.assertEqual(access_challenge(DENIAL),'akamai_access_denied')
        self.assertEqual(access_challenge(DENIAL.encode()),'akamai_access_denied')

    def test_encoded_provider_url_in_observed_error_is_recognized(self):
        encoded=DENIAL.replace('https://errors.edgesuite.net/',
            'https&#58;&#47;&#47;errors&#46;edgesuite&#46;net&#47;')
        self.assertEqual(access_challenge(encoded),'akamai_access_denied')

    def test_article_quoting_an_access_error_is_not_a_challenge(self):
        self.assertIsNone(access_challenge(DENIAL.replace('<body>','<body><main>').replace('</body>','</main></body>')))
        self.assertIsNone(access_challenge('<html><title>Access Denied</title><article>'+DENIAL+'</article></html>'))

    def test_missing_provider_marker_or_notice_is_not_this_access_shape(self):
        self.assertIsNone(access_challenge(DENIAL.replace('https://errors.edgesuite.net/TEST-ONLY','')))
        self.assertIsNone(access_challenge(DENIAL.replace("You don't have permission to access",'Documentation example')))

    def configure(self,site,*,list_raw='listing',detail_raw=DENIAL):
        cls=CRAWLERS[site];c=cls(None,delay=0);self.addCleanup(c._session.close)
        calls=[]
        item={'url':'https://www.accc.gov.au/doc/controlled','title':'Controlled source title',
              'published_date':'2026-10-01','date_raw':'1 Oct 2026','snippet':'A long source snippet. '*10}
        if site.endswith('about-us'):
            c._fetch_list_page=lambda page:(calls.append('list'),list_raw)[1]
            c._parse_list_page=lambda raw:[item]
            c._fetch_detail=lambda url:(calls.append('detail'),detail_raw)[1]
            c._parse_detail=lambda *args: (_ for _ in ()).throw(AssertionError('denied detail parsed'))
        else:
            module=cls.crawl.__globals__
            def fetch(url):
                kind='list' if '/search?' in url else 'detail';calls.append(kind)
                return list_raw if kind=='list' else detail_raw
            self.enterContext(patch.dict(module,{'_curl_get':fetch,'_parse_list_page':lambda raw:[item],
                '_parse_detail_page':lambda *args:(_ for _ in ()).throw(AssertionError('denied detail parsed'))}))
        c._save_paper=lambda p:(_ for _ in ()).throw(AssertionError('denial saved as source content'))
        return c,calls

    def test_denied_first_list_stops_once_without_completion(self):
        for site in SITES:
            with self.subTest(site=site):
                c,calls=self.configure(site,list_raw=DENIAL)
                self.assertEqual(c.crawl(),0);self.assertEqual(calls,['list'])
                self.assertEqual(c.delivery_source_failure,'authentication_failed');self.assertFalse(c.delivery_exhausted)

    def test_denied_detail_is_not_replaced_by_successful_list_snippet(self):
        for site in SITES:
            with self.subTest(site=site):
                c,calls=self.configure(site)
                self.assertEqual(c.crawl(),0);self.assertEqual(calls,['list','detail'])
                self.assertEqual(c.delivery_source_failure,'authentication_failed');self.assertFalse(c.delivery_exhausted)

    def test_fetch_failure_does_not_scan_remaining_200_pages(self):
        for site in SITES:
            with self.subTest(site=site),contextlib.redirect_stdout(io.StringIO()):
                c,calls=self.configure(site,list_raw=None)
                self.assertEqual(c.crawl(),0);self.assertEqual(calls,['list'])
                self.assertEqual(c.delivery_source_failure,'fetch_failed')

    def test_missing_detail_cannot_certify_success_using_snippet(self):
        for site in SITES:
            with self.subTest(site=site),contextlib.redirect_stdout(io.StringIO()):
                c,calls=self.configure(site,detail_raw=None)
                self.assertEqual(c.crawl(),0);self.assertEqual(calls,['list','detail'])
                self.assertEqual(c.delivery_source_failure,'fetch_failed')


if __name__=='__main__':unittest.main()
