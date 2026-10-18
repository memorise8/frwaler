"""L3b-20261018: enumeration split for pbl-nl-en.
Listing /en/publications?...type[28]=28&page=N (0-based, 10 rows, newest first); past the
end the page keeps the .view-publications-overview wrapper without rows (listing_marker).
Detail = unchanged _parse_detail(); the /downloads/ PDF link is not resolved by HEAD
(network-free), everything else equals crawl(). Memory-only: no network or subprocess
(the end-to-end run uses a temporary SQLite)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'pbl-nl-en'
EMPTY = '<html><body><main><div class="view view-publications-overview"></div></main></body></html>'


def native_crawl(testcase, fixture):
    """crawl() with its curl helper served from the fixture (one listing page, no next link)."""
    crawler, _ = load(SITE)
    captured, calls, list_calls = [], [], []

    def transport(url, *args, **kwargs):
        calls.append(url)
        if url in fixture['details']:
            return fixture['details'][url]
        list_calls.append(url)
        return fixture['listing_body'] if len(list_calls) == 1 else EMPTY
    crawler._delay = 0
    crawler.detail_delay = 0
    crawler._last_save_created = True
    crawler._curl = transport
    crawler._curl_head = lambda url, timeout=20: url
    crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
    with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
        result = crawler.crawl(limit=10)
    testcase.assertEqual(len(captured), result)
    testcase.assertEqual(1, len(list_calls))
    return captured, calls


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        return native_crawl(self, fixture or self.fixture)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '&page=186'
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        self.assertFalse(error_page.extra.get('explicit_empty', False))

    def test_downloads_link_is_kept_without_head_request(self):
        crawler, _ = load(self.SITE)
        url = 'https://www.pbl.nl/en/publications/synthetic-two'
        html = self.fixture['details'][url].replace('/system/files/synthetic-two.pdf', '/en/downloads/synthetic-two-1234pdf')
        paper = crawler._enumeration_paper(html, {'url': url, 'teaser': '', 'title': 'Synthetic two'})
        self.assertEqual('https://www.pbl.nl/en/downloads/synthetic-two-1234pdf', paper['pdf_url'])
        self.assertEqual('990002', paper['external_id'])
        self.assertEqual('99002', paper['post_number'])
        self.assertNotIn('_curl_head', vars(crawler))

    def test_identity_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['990002', '990001'], [p['external_id'] for p in expected])
        self.assertEqual(['99002', '990001'], [p['post_number'] for p in expected])


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{SITE: (200, 'text/html', EMPTY)})


if __name__ == '__main__':
    unittest.main()
