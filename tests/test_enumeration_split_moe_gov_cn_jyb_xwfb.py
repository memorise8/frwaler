"""moe-gov-cn-jyb_xwfb: split (_enumeration_items / _enumeration_paper) must produce
exactly the papers crawl() saves; crawl() is unchanged. Listing = WAS search fragment (chnlid=271) for every page; crawl() reads pages 1-25 as
static index pages and 26+ from WAS (same rows, same parser)."""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'moe-gov-cn-jyb_xwfb'


def run_native(case, fixture):
    """crawl() with an in-memory transport: static first page -> fixture rows, later pages empty."""
    crawler, _ = load(SITE)
    captured, calls = [], []

    def transport(url, *args, **kwargs):
        calls.append(url)
        if url in fixture['details']:
            return fixture['details'][url]
        if url == 'http://www.moe.gov.cn/jyb_xwfb/s271/':
            return fixture['listing_body']
        return fixture['empty_body']
    crawler._delay = 0
    crawler._last_save_created = True
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    crawler._fetch_raw = transport
    with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
        result = crawler.crawl(limit=10)
    case.assertEqual(len(captured), result)
    return captured, calls


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        return run_native(self, fixture or self.fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '?channelid=254874&chnlid=271&page=3'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'},
                                           '<html><body><ul><li><a href="/">home</a></li></ul>maintenance</body></html>', url))
        self.assertEqual([], [i for i in error_page.items if 's271' in i.url])

    def test_identity_is_contentid_and_url_number(self):
        expected, _ = self.native()
        self.assertEqual(['1448325', '1443829'], [p['external_id'] for p in expected])
        self.assertEqual(['1448325', '1443829'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        body = self.fixture['empty_body']
        result, docs = run_split(SITE, end=lambda url: Response(200, {'Content-Type': 'text/html'}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))


if __name__ == '__main__':
    unittest.main()
