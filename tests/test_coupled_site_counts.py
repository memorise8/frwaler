"""Native loops with real persistence for five individually reviewed counters."""
import json
import os
import unittest
from unittest.mock import Mock, patch

from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract


class CoupledContract(SiteContract):
    def configured(self, c):
        self.addCleanup(c._session.close)
        return super().configured(c)

    def test_cancel_stops_at_two_and_repeat_saves_remainder(self):
        c = self.crawler(); c.delivery_should_cancel = lambda: self.count() >= 2
        with self.assertRaises(CrawlCancelled): c.crawl()
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.crawler().crawl(), 6)
        self.assertEqual(self.count(), 8)

    def test_cancel_before_listing_performs_no_fetch(self):
        c = self.crawler(); c.delivery_should_cancel = lambda: True
        name = {'arn-se-om-arn': '_curl_get', 'asiapacific-ca-media': '_fetch_list_html',
                'centralbank-ie-publication': '_curl_get', 'auri-re-kr-publication': '_fetch_list_page',
                'cancer-fr-catalogue-des-public': '_curl_get'}[self.SITE]
        owner = self.module if self.SITE == 'centralbank-ie-publication' else c
        with patch.object(owner, name) as fetch:
            with self.assertRaises(CrawlCancelled): c.crawl()
            fetch.assert_not_called()
        self.assertEqual(self.count(), 0)

    def test_failed_save_is_reported_separately_from_new_count(self):
        c = self.crawler(); original = c._save_paper; calls = []
        def save(paper):
            calls.append(1)
            if len(calls) == 3: raise RuntimeError('controlled save failure')
            return original(paper)
        with patch.object(c, '_save_paper', save):
            self.assertEqual(c.crawl(), 7)
        self.assertEqual(c.delivery_source_failure, 'item_failed')
        self.assertFalse(c.delivery_exhausted)


class ArnCounts(CoupledContract, unittest.TestCase):
    SITE = 'arn-se-om-arn'

    def crawler(self):
        c = self.configured(self.module.ArnSeOmArnCrawler(self.conn))
        c._curl_get = Mock(return_value='<html><body><main>'+''.join(
            f'<a href="/files/annual-{year}.pdf">Annual report {year}</a>' for year in range(2018, 2026))+'</main></body></html>')
        return c


class AsiaPacificCounts(CoupledContract, unittest.TestCase):
    SITE = 'asiapacific-ca-media'

    def crawler(self):
        c = self.configured(self.module.AsiaPacificCaMediaCrawler(self.conn))
        c._fetch_list_html = lambda: '<html>fixture list</html>'
        c._extract_article_paths = lambda html: [f'/media/news-releases/{n}' for n in range(1, 9)]
        c._fetch_detail = lambda path: {'title': 'Research '+path.rsplit('/', 1)[-1], 'abstract': 'Scientific research. '*10,
                                       'published_date': '2025-07-01', 'url': c.base_url+path, 'description': 'fixture'}
        return c


class CentralBankCounts(CoupledContract, unittest.TestCase):
    SITE = 'centralbank-ie-publication'

    def crawler(self):
        c = self.configured(self.module.CentralBankIEPublicationCrawler(self.conn))
        records = [{'documentName': f'Annual Report {year}', 'date': '29/05/2025', 'url': f'/docs/report-{year}.pdf'} for year in range(2018, 2026)]
        self.enterContext(patch.object(self.module, '_curl_get', return_value='var appData = '+json.dumps(records)+';'))
        self.enterContext(patch.object(self.module, '_fetch_detail_text', return_value='Scientific research. '*10))
        return c


class AuriCounts(CoupledContract, unittest.TestCase):
    SITE = 'auri-re-kr-publication'

    def crawler(self):
        c = self.configured(self.module.AuriReKrPublicationCrawler(self.conn))
        c._fetch_list_page = lambda page: [
            {'pub_id': str(n), 'url': f'https://www.auri.re.kr/publication/view.es?publication_id={n}', 'title': f'Research {n}',
             'first_author': '', 'date_raw': '2025.07.01', 'kw_list': [], 'dl_href': '', 'category': 'Research'}
            for n in {1: range(1, 6), 2: range(6, 9)}.get(page, [])]
        c._fetch_detail = lambda pub_id, **kwargs: {'title': f'Research {pub_id}', 'abstract': 'Scientific research. '*10}
        return c

    def test_list_failure_does_not_skip_to_later_page(self):
        c = self.crawler(); fetch = c._fetch_list_page; calls = []
        def failing(page):
            calls.append(page)
            if page == 2: raise RuntimeError('controlled missing page')
            return fetch(page)
        c._fetch_list_page = failing
        self.assertEqual(c.crawl(), 5)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')
        self.assertEqual(calls, [1, 2])


class CancerCounts(CoupledContract, unittest.TestCase):
    SITE = 'cancer-fr-catalogue-des-public'

    def crawler(self):
        c = self.configured(self.module.CancerFrCataloguePublicCrawler(self.conn))
        c._curl_get = lambda url: url
        def listing(raw, url):
            second = url.endswith('?page=2')
            return ([{'url': f'https://www.cancer.fr/research/{n}', 'n': n} for n in (range(6, 9) if second else range(1, 6))],
                    None if second else c._START_URL+'?page=2')
        c._parse_list_page = listing
        c._fetch_detail_record = lambda item: {'external_id': str(item['n']), 'url': item['url'], 'title': f"Research {item['n']}", 'abstract': 'Scientific research. '*10}
        c._algolia_page = Mock(return_value={'hits': []})
        return c

    def test_all_known_primary_records_do_not_trigger_fallback_search(self):
        self.assertEqual(self.crawler().crawl(), 8)
        c = self.crawler()
        self.assertEqual(c.crawl(), 0)
        c._algolia_page.assert_not_called()

    def test_primary_fetch_failure_is_not_masked_by_fallback(self):
        c = self.crawler(); c._curl_get = lambda url: None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')
        c._algolia_page.assert_not_called()

    def test_fallback_path_also_counts_only_new_rows(self):
        for expected in (3, 3, 2, 0):
            c = self.crawler(); c._parse_list_page = lambda raw, url: ([], None)
            c._ALGOLIA_QUERIES = ['fixture-query']
            c._algolia_page = lambda query, page: {'hits': [{'url': f'https://www.cancer.fr/research/{n}', 'n': n} for n in {0: range(1, 6), 1: range(6, 9)}.get(page, [])], 'nbPages': 2}
            c._candidate_from_algolia = lambda hit, query: hit
            c._is_target_collection = lambda category: True
            self.assertEqual(c.crawl(limit=3), expected)
        self.assertEqual(self.count(), 8)


for cls in (ArnCounts, AsiaPacificCounts, CentralBankCounts, AuriCounts, CancerCounts):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
