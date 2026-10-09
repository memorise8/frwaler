"""Native source loops with controlled inputs; real SQLite/PostgreSQL persistence."""
import os
import unittest
from unittest.mock import Mock, patch

from tests.test_coupled_site_counts import CoupledContract


def record(n, host):
    return {'external_id': str(n), 'post_number': str(n), 'url': f'https://{host}/article/{n}',
            'title': f'Research {n}', 'abstract': 'Scientific research. '*10}


class LlnlCounts(CoupledContract, unittest.TestCase):
    SITE = 'llnl-gov-news'; LIST_METHOD = '_fetch_page'; LIST_MODULE = True

    def crawler(self):
        c = self.configured(self.module.LLNLGovNewsCrawler(self.conn))
        self.enterContext(patch.object(self.module, '_PAGE_SIZE', 3))
        self.records = getattr(self, 'records', [{'nid': str(n), 'title': f'Research {n}', 'alias_path': f'/news/{n}',
                       'body_value': 'Scientific research. '*10} for n in range(1, 9)])
        self.fetch = self.enterContext(patch.object(self.module, '_fetch_page', side_effect=lambda offset, size: self.records[offset:offset+size]))
        return c

    def test_short_windows_do_not_skip_records(self):
        c = self.crawler()
        self.fetch.side_effect = lambda offset, size: self.records[offset:offset+min(size, 2)]
        self.assertEqual(c.crawl(), 8)
        self.assertEqual([call.args[0] for call in self.fetch.call_args_list], [0, 2, 4, 6, 8])

    def test_filtered_page_does_not_stop_source_traversal(self):
        c = self.crawler()
        for item in self.records[:3]: item['body_value'] = 'short'
        self.assertEqual(c.crawl(), 5)

    def test_repeated_page_is_failed_not_exhausted(self):
        c = self.crawler(); self.fetch.side_effect = None; self.fetch.return_value = self.records[:3]
        self.assertEqual(c.crawl(), 3)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.fetch.call_count, 2)

    def test_distinct_source_ids_with_shared_fallback_url_are_preserved(self):
        c = self.crawler()
        for item in self.records: item.pop('alias_path')
        self.assertEqual(c.crawl(), 8)
        self.assertEqual(self.count(), 8)


class NindsCounts(CoupledContract, unittest.TestCase):
    SITE = 'ninds-nih-gov-publications'; LIST_METHOD = '_curl_get'

    def crawler(self):
        c = self.configured(self.module.NINDSNIHPublicationsCrawler(self.conn))
        c._ITEMS_PER_PAGE = 3
        c._curl_get = Mock(side_effect=lambda url: url+' Displaying 1 - 3 of 8')
        def listing(html):
            page = int(html.split('page=')[1].split()[0])
            return [dict(record(n, 'www.ninds.nih.gov'), slug=str(n), description='Scientific research. '*10,
                         date_str='', pdf_url=None) for n in range(1+page*3, min(9, 4+page*3))]
        c._parse_listing = listing
        c._parse_detail = lambda html, url: {'abstract': 'Scientific research. '*10}
        return c

    def test_small_new_limit_does_not_prefetch_later_listing_pages(self):
        c = self.crawler()
        self.assertEqual(c.crawl(limit=1), 1)
        listings = [call.args[0] for call in c._curl_get.call_args_list if '?page=' in call.args[0]]
        self.assertEqual(len(listings), 1)


class SanteCounts(CoupledContract, unittest.TestCase):
    SITE = 'sante-gouv-fr-ministere'; LIST_METHOD = '_make_page'

    def crawler(self):
        c = self.configured(self.module.SanteGouvFrMinistereCrawler(self.conn))
        c._make_page = Mock(return_value=object()); c._close_browser = Mock()
        c._fetch = lambda page, url: '<main>Controlled public source fixture</main>'
        c._get_category_urls = lambda html: ['https://sante.gouv.fr/category']
        urls = [record(n, 'sante.gouv.fr')['url'] for n in range(1, 9)]
        c._collect_article_urls = lambda page, cat_url, max_count=None, **kwargs: urls[:max_count] if max_count is not None else urls
        c._parse_detail = lambda html, url: record(int(url.rsplit('/', 1)[1]), 'sante.gouv.fr')
        return c

    def listing_crawler(self):
        c = self.crawler(); del c._collect_article_urls
        def fetch(page, url):
            if '/category' in url:
                expanded = '?max_articles=' in url
                links = ''.join(f'<a href="https://sante.gouv.fr/article/{n}">Research</a>' for n in (range(1,9) if expanded else range(1,4)))
                more = '' if expanded else '<a href="?max_articles=8">More</a>'
                return '<main>'+links+more+'</main>'
            return '<main>Controlled article or index</main>'
        c._fetch = Mock(side_effect=fetch)
        return c

    def test_cumulative_listing_streams_past_known_items(self):
        for expected, pages in ((3,1),(3,2),(2,2),(0,2)):
            c = self.listing_crawler()
            self.assertEqual(c.crawl(limit=3), expected)
            self.assertEqual(sum('/category' in call.args[1] for call in c._fetch.call_args_list), pages)
            c._close_browser.assert_called_once()
        self.assertEqual(self.count(), 8)

    def test_browser_is_closed_on_cancellation(self):
        from crawler.base_crawler import CrawlCancelled
        c = self.listing_crawler(); c.delivery_should_cancel = lambda: self.count() >= 2
        with self.assertRaises(CrawlCancelled): c.crawl()
        c._close_browser.assert_called_once()
        self.assertEqual(self.count(), 2)

    def test_nonadvancing_load_more_is_not_reported_as_exhausted(self):
        c = self.listing_crawler()
        c._fetch = lambda page,url: '<main><a href="https://sante.gouv.fr/article/1">Research</a><a href="?max_articles=8">More</a></main>'
        self.assertEqual(c.crawl(), 1)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertFalse(c.delivery_exhausted)
        c._close_browser.assert_called_once()


class SwissCounts(CoupledContract, unittest.TestCase):
    SITE = 'swisspolar-ch-expeditions'; LIST_METHOD = '_fetch_list_page'

    def crawler(self):
        c = self.configured(self.module.SwissPolarChExpeditionsCrawler(self.conn))
        c._fetch_list_page = lambda page: ([record(n, 'swisspolar.ch') for n in {1:range(1,6),2:range(6,9)}.get(page,[])], page == 1)
        c._fetch_detail = lambda item: {}
        c._build_paper = lambda item, detail, index: dict(item)
        return c


class VaCounts(CoupledContract, unittest.TestCase):
    SITE = 'va-gov-vetdata'; LIST_METHOD = '_curl_get'

    def crawler(self):
        c = self.configured(self.module.VaGovVetdataCrawler(self.conn))
        c._curl_get = lambda url: '<main>Controlled source fixture</main>'
        c._parse_reports = lambda html: [record(n, 'www.va.gov') for n in range(1, 9)]
        return c


for cls in (LlnlCounts, NindsCounts, SanteCounts, SwissCounts, VaCounts):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
