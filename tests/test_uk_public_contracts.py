"""Public UK native parsers with real persistence; no live completeness claim."""
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.test_coupled_site_counts import CoupledContract

DATES = [('2025', None, 'year', 'partial'), ('July 2025', None, 'month', 'partial'),
         ('2025-02-30', None, 'day', 'invalid'),
         ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
         ('2025-07-11 wrong suffix', None, None, 'unrecognized'),
         ('03/04/2025', None, 'day', 'ambiguous'),
         ('11 July 2025', '2025-07-11', 'day', 'normalized'),
         ('2025-07', None, 'month', 'partial')]


class DateContract:
    def test_native_raw_dates_survive_save_and_repeat(self):
        self.assertEqual(self.crawler().crawl(), 8)
        before = self.identities()
        self.assertEqual(self.crawler().crawl(), 0)
        self.assertEqual(self.identities(), before)
        rows = self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date,listed_date_raw,listed_date_precision,listed_date_status FROM documents ORDER BY seq_id').fetchall()
        for row, (raw, value, precision, status) in zip(rows, DATES):
            actual = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            with self.subTest(raw=raw):
                self.assertEqual(actual, (value, raw, precision, status)*2)


class DataGovCounts(DateContract, CoupledContract, unittest.TestCase):
    SITE = 'data-gov-uk-search'; LIST_METHOD = '_curl_get'

    def crawler(self):
        c = self.configured(self.module.DataGovUKSearchCrawler(self.conn))
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            if 'package_show' in url:
                n = int(query['id'][0].rsplit('-', 1)[1])
                return json.dumps({'success': True, 'result': {'id': f'record-{n}', 'name': f'record-{n}',
                    'title': f'Research {n}', 'notes': 'Scientific research. '*10,
                    'metadata_created': '2000-01-01', 'metadata_modified': DATES[n-1][0],
                    'extras': [{'key': 'dcat_issued', 'value': DATES[n-1][0]}]}})
            page = int(query.get('page', ['1'])[0])
            ids = {1: range(1, 6), 2: range(6, 9)}.get(page, [])
            return '<main>'+''.join(f'<div class="dgu-results__result"><h2><a href="/dataset/record-{n}">Research {n}</a></h2></div>' for n in ids)+('<a rel="next" href="/search?page=2">Next</a>' if page == 1 else '')+'</main>'
        c._curl_get = fetch
        return c

    def test_unavailable_detail_is_failure_not_exclusion(self):
        c = self.crawler(); c._fetch_package = lambda item: None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')

    def test_api_failure_is_not_empty_result(self):
        for raw in (None, 'not-json', '[]', '{"success":false}', '{"success":true,"result":{}}'):
            with self.subTest(raw=raw):
                c = self.crawler(); c._curl_get = lambda *args, **kwargs: raw
                self.assertEqual(c._fetch_api_list_page(1), ([], False, None))
                self.assertIsNotNone(c.delivery_source_failure)

    def test_valid_api_empty_result_is_not_failure(self):
        c = self.crawler(); c._curl_get = lambda *args, **kwargs: json.dumps({'success': True, 'result': {'results': [], 'count': 0}})
        self.assertEqual(c._fetch_api_list_page(1), ([], False, None))
        self.assertFalse(c.delivery_source_failure)

    def test_failed_listing_is_not_empty_success(self):
        c = self.crawler(); c._curl_get = lambda *args, **kwargs: None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')

    def test_api_fallback_repeats_count_only_new_documents(self):
        self.enterContext(patch.object(self.module, '_ROWS_PER_PAGE', 5))
        for expected in (3, 3, 2, 0):
            c = self.crawler(); original = c._curl_get
            def fetch(url, **kwargs):
                if 'package_show' in url:
                    return original(url, **kwargs)
                if 'package_search' not in url:
                    return '<main>No HTML rows</main>'
                start = int(parse_qs(urlsplit(url).query)['start'][0])
                return json.dumps({'success': True, 'result': {'count': 8, 'results': [
                    {'id': f'record-{n}', 'name': f'record-{n}', 'title': f'Research {n}'}
                    for n in {0: range(1, 6), 5: range(6, 9)}.get(start, [])]}})
            c._curl_get = fetch
            self.assertEqual(c.crawl(limit=3), expected)
            self.assertFalse(c.delivery_source_failure)
        self.assertEqual(self.count(), 8)


class OnsCounts(DateContract, CoupledContract, unittest.TestCase):
    SITE = 'ons-gov-uk-search'; LIST_METHOD = '_curl_get'; LIST_MODULE = True

    def crawler(self):
        c = self.configured(self.module.OnsGovUkSearchCrawler(self.conn))
        def fetch(url, **kwargs):
            if '/v1/search' in url:
                offset = int(parse_qs(urlsplit(url).query)['offset'][0])
                return json.dumps({'items': [{'uri': f'/datasets/research-{n}', 'title': f'Research {n}',
                    'summary': 'Scientific research. '*10, 'release_date': DATES[n-1][0]} for n in {0: range(1, 6), 10: range(6, 9)}.get(offset, [])]})
            return json.dumps({'description': {'summary': 'Scientific research. '*10}, 'downloads': []})
        self.fetch = self.enterContext(patch.object(self.module, '_curl_get', side_effect=fetch))
        return c

    def test_list_failure_or_invalid_shape_is_not_empty_success(self):
        for data in (None, [], {}, {'items': {}}, {'items': [None]}):
            with self.subTest(data=data):
                c = self.crawler(); c._search_page = lambda *args: data
                self.assertEqual(c.crawl(), 0)
                self.assertIsNotNone(c.delivery_source_failure)
                self.assertFalse(c.delivery_exhausted)

    def test_failed_detail_does_not_silently_erase_file_metadata(self):
        c = self.crawler(); c._fetch_detail = lambda *args: None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')


class GovScotCounts(DateContract, CoupledContract, unittest.TestCase):
    SITE = 'gov-scot-statistics-and-resea'; LIST_METHOD = '_curl_get'; LIST_MODULE = True

    def crawler(self):
        c = self.configured(self.module.GovScotStatisticsCrawler(self.conn))
        def fetch(url, **kwargs):
            if url.endswith('/documents/'):
                return '<main>No files</main>'
            if '/publications/' in url:
                n = int(url.rstrip('/').rsplit('-', 1)[1])
                return '<h1 class="ds_page-header__title">Research '+str(n)+'</h1><script type="application/ld+json">'+json.dumps({'@type': 'Article', 'description': 'Scientific research. '*10, 'datePublished': DATES[n-1][0]})+'</script>'
            page = int(parse_qs(urlsplit(url).query)['page'][0])
            return '<main>'+''.join(f'<li class="ds_search-result"><a class="ds_search-result__link" href="/publications/research-{n}/">Research</a><dd class="ds_metadata__value">{DATES[n-1][0]}</dd></li>' for n in {1: range(1, 6), 2: range(6, 9)}.get(page, []))+'</main>'
        self.fetch = self.enterContext(patch.object(self.module, '_curl_get', side_effect=fetch))
        return c

    def test_failed_detail_is_not_exclusion(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: None if '/publications/' in url else original(url, **kw)
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')

    def test_failed_listing_is_not_empty_success(self):
        c = self.crawler(); self.fetch.side_effect = lambda *args: None
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')


for cls in (DataGovCounts, OnsCounts, GovScotCounts):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
