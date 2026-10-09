import json
import os
import re
import unittest
from unittest.mock import patch

from tests.test_coupled_site_counts import CoupledContract


class CsoCounts(CoupledContract, unittest.TestCase):
    SITE = 'cso-ie-en'; LIST_METHOD = '_curl_get'; LIST_MODULE = True

    def crawler(self):
        c = self.configured(self.module.CsoIeEnCrawler(self.conn))
        year = self.module.datetime.now().year
        self.years = getattr(self, 'years', {year: range(1, 6), year-1: range(6, 9)})
        self.enterContext(patch.object(self.module, '_EARLIEST_YEAR', year-2))
        self.enterContext(patch.object(self.module, '_curl_get', side_effect=lambda url: url))
        def listing(url):
            current = int(re.search(r'/(\d{4})pressreleases/', url)[1])
            return [{'node_id': str(n), 'url': f'https://www.cso.ie/research/{n}', 'title': f'Research {n}', 'listed_date': '2025-07-01'} for n in self.years.get(current, [])]
        self.enterContext(patch.object(self.module, '_parse_list_page', side_effect=listing))
        self.enterContext(patch.object(self.module, '_extract_detail', return_value={'abstract': 'Scientific research. '*10}))
        return c

    def test_empty_year_does_not_hide_older_records(self):
        year = self.module.datetime.now().year
        self.years = {year: range(1, 4), year-2: range(4, 9)}
        self.assertEqual(self.crawler().crawl(), 8)
        self.assertEqual(self.count(), 8)

    def test_failed_year_does_not_skip_to_older_year(self):
        c = self.crawler()
        with patch.object(self.module, '_curl_get', return_value=None) as fetch:
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')


class EmCounts(CoupledContract, unittest.TestCase):
    SITE = 'em-dk-aktuelt'; LIST_METHOD = '_discover_month_paths'

    def crawler(self):
        c = self.configured(self.module.EmDkAktueltCrawler(self.conn))
        c._discover_month_paths = lambda: ['/aktuelt/2025/07']
        urls = [f'https://www.em.dk/research/{n}' for n in range(1, 9)]
        c._fetch_month_item_urls = lambda month, max_items=None, deadline=None: urls[:max_items] if max_items is not None else urls
        self.enterContext(patch.object(self.module, '_curl_get', side_effect=lambda url: url))
        self.enterContext(patch.object(self.module, '_parse_detail', side_effect=lambda url, html: {'external_id': url.rsplit('/', 1)[-1], 'url': url, 'title': 'Research', 'abstract': 'Scientific research. '*10}))
        return c

    def real_month_crawler(self, pages):
        c = self.crawler()
        del c._fetch_month_item_urls
        self.enterContext(patch.object(self.module, '_extract_itemlist_config', return_value={'fixture': True}))
        self.enterContext(patch.object(self.module, '_extract_item_urls', side_effect=lambda html: html.split(',')))
        proxy = self.enterContext(patch.object(self.module, '_curl_post_proxy', side_effect=pages))
        return c, proxy

    @staticmethod
    def page(numbers, last=False):
        return {'success': True, 'value': {'page': ','.join(f'https://www.em.dk/research/{n}' for n in numbers), 'lastPage': last}}

    def test_month_pages_are_lazy_and_known_pages_do_not_consume_limit(self):
        pages = [self.page(range(1, 4)), self.page(range(4, 7)), self.page(range(7, 9), True)]
        c, proxy = self.real_month_crawler(pages)
        self.assertEqual(c.crawl(limit=3), 3)
        self.assertEqual(proxy.call_count, 1)
        before = self.identities()
        c, proxy = self.real_month_crawler(pages)
        self.assertEqual(c.crawl(limit=3), 3)
        self.assertEqual(proxy.call_count, 2)
        after = self.identities()
        self.assertTrue(all(after[k] == v for k,v in before.items()))
        c, proxy = self.real_month_crawler(pages)
        self.assertEqual(c.crawl(limit=3), 2)
        self.assertEqual(proxy.call_count, 3)
        self.assertEqual(self.count(), 8)

    def test_month_failure_and_repetition_are_not_exhaustion(self):
        for failure, expected in ((None, 'fetch_failed'), ({'success': True, 'value': {}}, 'invalid_response'),
                                  (self.page(range(1, 4)), 'invalid_response')):
            with self.subTest(reason=expected):
                c, proxy = self.real_month_crawler([self.page(range(1, 4)), failure])
                c.crawl()
                self.assertEqual(c.delivery_source_failure, expected)
                self.assertFalse(c.delivery_exhausted)
                self.assertEqual(proxy.call_count, 2)

    def test_expired_month_budget_does_not_request_proxy_page(self):
        c, proxy = self.real_month_crawler([])
        self.assertEqual(list(c._fetch_month_item_urls('/fixture', deadline=0)), [])
        proxy.assert_not_called()


class IcpCounts(CoupledContract, unittest.TestCase):
    SITE = 'icp-fr-a-propos-de-licp'; LIST_METHOD = '_curl_get'

    def crawler(self):
        c = self.configured(self.module.ICPFrAPropsDeLICPCrawler(self.conn))
        c._curl_get = lambda url: '<main>fixture list</main>'
        c._parse_list = lambda soup: [{'external_id': str(n), 'url': f'https://www.icp.fr/research/{n}', 'pdf_url': f'https://www.icp.fr/files/{n}.pdf', 'title': f'Research {n}'} for n in range(1, 9)]
        c._extract_pdf = lambda url: ('Scientific research. '*10, url.rsplit('/', 1)[-1])
        return c


class InternationalCounts(CoupledContract, unittest.TestCase):
    SITE = 'international-gc-ca-transparency-transpa'; LIST_METHOD = '_curl_get'

    def crawler(self):
        c = self.configured(self.module.InternationalGCTransparencyTranspaCrawler(self.conn))
        c._curl_get = lambda url, **kwargs: '<main>fixture</main>'
        c._parse_list = lambda soup: [{'url': f'https://international.gc.ca/research/{n}', 'n': n} for n in range(1, 9)]
        def detail(soup, record):
            fields = dict.fromkeys(['published_date','listed_date','authors','publisher','department','pdf_url','keywords','category','original_filename'])
            return dict(fields, external_id=str(record['n']), post_number=str(record['n']), title=f"Research {record['n']}", url=record['url'], abstract='Scientific research. '*10, metadata={})
        c._parse_detail = detail
        return c


class NinaCounts(CoupledContract, unittest.TestCase):
    SITE = 'nina-no-english'; LIST_METHOD = '_curl_cffi_get'

    def crawler(self):
        c = self.configured(self.module.NINAOlderSeriesCrawler(self.conn))
        records = [{'title': 'Scientific research with a sufficiently detailed title about regional wildlife and its changing habitats '+str(n),
                    'publicationYear': 2025, 'serie': f'NINA rapport {n}', 'fileOrLink': {'url': f'https://www.nina.no/files/{n}.pdf'}} for n in range(1, 9)]
        c._curl_cffi_get = lambda url: json.dumps(records)
        return c

    def test_invalid_json_is_distinguished_from_an_empty_list(self):
        for raw in ('not JSON', '{"error":"upstream failure"}'):
            c = self.crawler(); c._curl_cffi_get = lambda url: raw
            self.assertEqual(c.crawl(), 0)
            self.assertEqual(c.delivery_source_failure, 'invalid_response')
        c = self.crawler(); c._curl_cffi_get = lambda url: '[]'
        self.assertEqual(c.crawl(), 0)
        self.assertIsNone(c.delivery_source_failure)


for cls in (CsoCounts, EmCounts, IcpCounts, InternationalCounts, NinaCounts):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
