"""spo-go-kr-site: board rows (GET List.do pageIndex) + View.do page through the unchanged
crawl() (POST list pages) vs the plan adapter. Memory only.

crawl() saves the pinned ``li.noti`` postings once from page 1 (seen_urls); the plan keeps
them only on page 1, so later pages do not repeat them.
"""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'spo-go-kr-site'
LIST = 'https://www.spo.go.kr/site/spo/ex/board/List.do?cbIdx=1303&pageIndex='


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; pages = []

        def list_page(page, *args, **kwargs):
            pages.append(page)
            return (fixture['listing_body'] if page == 1 else fixture['empty_body']).encode()

        def detail(url, *args, **kwargs):
            calls.append(url)
            return fixture['details'][url].encode()

        def no_header_request(*args, **kwargs):
            raise AssertionError('fixture attachments carry filenames')

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._fetch_list_page = list_page
        crawler._curl_get = detail
        crawler._curl_content_disposition_filename = no_header_request
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(pages), 2)
        return captured, calls

    def test_identity_is_bc_idx(self):
        papers = self.pipeline()
        self.assertEqual(self.fixture['expected_ids'], [p['post_number'] for p in papers])
        self.assertEqual(list(self.fixture['details']), [p['url'] for p in papers])

    def test_pinned_rows_only_on_first_page(self):
        crawler, _ = load(self.SITE)
        with guarded():
            first = crawler._enumeration_items(self.fixture['listing_body'], LIST + '1')
            bare = crawler._enumeration_items(self.fixture['listing_body'], LIST[:-len('&pageIndex=')])
            later = crawler._enumeration_items(self.fixture['listing_body'], LIST + '7')
        self.assertEqual(self.fixture['expected_ids'], [r['external_id'] for r in first])
        self.assertEqual(first, bare)
        self.assertEqual([i for i in self.fixture['expected_ids'] if i not in self.fixture['pinned_ids']],
                         [r['external_id'] for r in later])
        self.assertEqual([7], [r['list_page'] for r in later])

    def test_detail_fetchers_are_restored(self):
        crawler, _ = load(self.SITE)
        url = next(iter(self.fixture['details']))
        item = crawler._enumeration_items(self.fixture['listing_body'], LIST + '1')[0]
        with guarded():
            crawler._enumeration_paper(self.fixture['details'][url], item)
        self.assertNotIn('_curl_get', crawler.__dict__)
        self.assertNotIn('_curl_content_disposition_filename', crawler.__dict__)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE
