"""hec-edu-en: crawl() lists through Drupal Views AJAX (POST); the plan reads the
same view as GET ?page=N. The native run feeds crawl() the fixture cards as the
AJAX insert command, so the comparison covers the same card HTML both ways."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import load


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'hec-edu-en'

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; ajax_pages = []

        def get(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            return fixture['empty_body']  # list page read only for the view_dom_id

        def ajax(page, view_dom_id, *args, **kwargs):
            ajax_pages.append(page)
            data = fixture['listing_body'] if page == 0 else fixture['empty_body']
            return json.dumps([{'command': 'settings'}, {'command': 'insert', 'data': data}])

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = get
        crawler._ajax_post = ajax
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(ajax_pages), 2)
        return captured, calls


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'hec-edu-en'
