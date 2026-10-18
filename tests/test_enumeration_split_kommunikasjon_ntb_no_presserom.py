"""L3b-20261018: enumeration split for kommunikasjon-ntb-no-presserom.
One list API call /public-website-api/pressroom/17848243/releases/1000/0?categories=19
holds the whole category (paging.count); crawl() reads the same list 20 per page.
The plan's ?page=N is ignored by the API, so pages after the start page are declared empty.
Memory-only: no network or subprocess (the end-to-end run uses a temporary SQLite)."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'kommunikasjon-ntb-no-presserom'
EMPTY = json.dumps({'paging': {'count': 2, 'itemsPerPage': 1000, 'page': 0}, 'releases': []})


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        # crawl() fetches through self._curl_text (curl subprocess); serve the fixture instead.
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, list_calls = [], [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return json.dumps(fixture['details'][url], ensure_ascii=False)
            list_calls.append(url)
            return fixture['listing_body'] if len(list_calls) == 1 else '[]'
        crawler._detail_delay = 0
        crawler._last_save_created = True
        crawler._curl_text = transport
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(2, len(list_calls))
        return captured, calls

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure, Response
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?categories=19&page=2'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, self.fixture['listing_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '[]', url))

    def test_truncated_list_is_a_parse_error(self):
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        crawler, _ = load(self.SITE)
        data = json.loads(self.fixture['listing_body'])
        data['paging']['count'] = 5
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items(data, self.plan['sites'][SITE]['listing']['url'] + '?categories=19&page=1')

    def test_post_number_is_release_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['9000002', '9000001'], [p['external_id'] for p in expected])
        self.assertEqual(['9000002', '9000001'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'application/json', EMPTY)})


if __name__ == '__main__':
    unittest.main()
