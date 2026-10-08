import json
import subprocess
import unittest
from unittest import mock

from crawler.sites import CRAWLERS


class DoajFailureContractTest(unittest.TestCase):
    def setUp(self):
        self.crawler = CRAWLERS['doaj-org-search'](db_conn=None, delay=0)

    def test_malformed_or_error_response_never_claims_exhaustion(self):
        for raw in ['<html>blocked</html>', 'null', '[]', '{"error":"blocked"}', '{"results":{}}']:
            with self.subTest(raw=raw):
                crawler = type(self.crawler)(db_conn=None, delay=0)
                with mock.patch.object(crawler, '_curl_get', return_value=raw):
                    crawler.crawl()
                self.assertEqual(crawler.delivery_source_failure, 'invalid_response')
                self.assertFalse(crawler.delivery_exhausted)

    def test_curl_nonzero_response_body_is_not_treated_as_success(self):
        result = subprocess.CompletedProcess([], 22, stdout=b'{"results":[]}', stderr=b'')
        with mock.patch('subprocess.run', return_value=result) as run:
            self.assertIsNone(self.crawler._curl_get('https://example.test', retries=1))
        self.assertIn('-skf', run.call_args.args[0])

    def test_failed_item_does_not_advance_past_its_page(self):
        raw = json.dumps({'results': [None]})
        with mock.patch.object(self.crawler, '_curl_get', return_value=raw):
            self.crawler.crawl()
        self.assertEqual(self.crawler.delivery_source_failure, 'item_failed')
        self.assertIsNone(self.crawler.delivery_pending_cursor)
        self.assertFalse(self.crawler.delivery_exhausted)

    def test_limit_in_middle_of_page_does_not_skip_remaining_items(self):
        item = {'id': '1', 'bibjson': {'title': 'Test', 'abstract': 'a' * 120}}
        raw = json.dumps({'results': [item, {**item, 'id': '2'}]})
        with mock.patch.object(self.crawler, '_curl_get', return_value=raw), mock.patch.object(self.crawler, '_save_paper', side_effect=lambda _: setattr(self.crawler, '_last_save_created', True)) as save:
            self.crawler.crawl(limit=1)
        save.assert_called_once()
        self.assertEqual(self.crawler.delivery_pending_cursor, {'page': 1, 'offset': 1})
        self.assertIsNone(self.crawler.delivery_source_failure)
        self.assertEqual(self.crawler.delivery_cursor_items_done, 1)
        resumed = type(self.crawler)(db_conn=None, delay=0)
        resumed.delivery_cursor = self.crawler.delivery_pending_cursor
        with mock.patch.object(resumed, '_curl_get', return_value=raw), mock.patch.object(resumed, '_save_paper', side_effect=lambda _: setattr(resumed, '_last_save_created', True)) as save:
            resumed.crawl(limit=1)
        self.assertEqual(save.call_args.args[0]['external_id'], '2')
        self.assertEqual(resumed.delivery_pending_cursor, {'page': 2})
        self.assertEqual(resumed.delivery_cursor_items_done, 1)
