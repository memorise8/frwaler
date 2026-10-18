"""kammarkollegiet-se-aktuellt: /aktuellt embeds every news item in one page and the
server ignores page numbers. crawl() reads that one page; last_page_backward sees
page 2 repeat page 1 ("wrap") and ends there (the fixture's ``empty_body`` is the
same full page the server returns for any page number)."""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'kammarkollegiet-se-aktuellt'
LIST_URL = 'https://www.kammarkollegiet.se/aktuellt'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []

        def fetch(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url].encode('utf-8')
            self.assertEqual(LIST_URL, url)
            return fixture['listing_body'].encode('utf-8')

        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_fetch', fetch))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(1, calls.count(LIST_URL))
        return captured, calls

    def _parser(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        return ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        """There is no empty page on this source: any page number repeats the full
        list (ended as "wrap", see the run test); a page without the embedded
        news JSON is a listing parse error, never an empty page."""
        parser = self._parser()
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], LIST_URL + '?page=2'))
        self.assertEqual(2, len(page.items))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><main>maintenance</main></html>', LIST_URL + '?page=2'))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_first_page_with_and_without_page_number_lists_all(self):
        parser = self._parser()
        for url in (LIST_URL, LIST_URL + '?page=1'):
            page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url))
            self.assertEqual(2, len(page.items), url)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
