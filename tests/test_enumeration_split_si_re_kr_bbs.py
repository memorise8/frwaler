"""si-re-kr-bbs: board list rows + detail page through the unchanged crawl() vs the plan adapter.

crawl() pages /bbs/list.do?key=2024100039&pageIndex=N&orderBy=bbsOrdr desc (10 rows/page)
through ``self._session`` and converts each /bbs/view.do?key=…&pstSn=N page inline.
The plan reads the same pages with ``_enumeration_items(html, url)`` and converts
details with ``_enumeration_paper(html, item)``; the saved paper (external_id = pstSn)
must be identical. A page past the last one keeps ``div.board-count`` and shows
``li.no-list``.
"""
import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlencode

from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'si-re-kr-bbs'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() fetches through requests.Session.get(url, params=...); serve it from memory."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; listing_calls = []

        def get(url, params=None, headers=None, timeout=None):
            full = url + ('?' + urlencode(params) if params else '')
            calls.append(full)
            if full in fixture['details']:
                body = fixture['details'][full]
            else:
                listing_calls.append(full)
                body = fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
            return SimpleNamespace(content=body.encode('utf-8'), apparent_encoding='utf-8',
                                   raise_for_status=lambda: None)

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._session = SimpleNamespace(get=get)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE
