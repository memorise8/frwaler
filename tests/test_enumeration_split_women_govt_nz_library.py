import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

import tests.collector_split_helpers as base
from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = 'women-govt-nz-library'

    def native(self, fixture=None):
        """Unchanged crawl(): the list comes from a Views AJAX POST (replaceWith
        fragment = the view block of the GET page) and details are bytes."""
        fixture = fixture or self.fixture
        crawler, module = base.load(self.SITE)
        captured, calls, list_calls = [], [], []

        def views_ajax(url, fields, retries=3):
            list_calls.append(fields.get('page'))
            calls.append(url)
            if len(list_calls) == 1:
                fragment = fixture['listing_body']
            else:
                fragment = fixture['empty_body']
            return json.dumps([{'command': 'insert', 'method': 'replaceWith', 'data': fragment}]).encode()

        def get(url, retries=3):
            calls.append(url)
            return fixture['details'][url].encode() if url in fixture['details'] else None

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl_post', views_ajax), patch.object(module, '_curl_get', get), \
                patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(['0', '1'], list_calls)
        return captured, calls


class RegisteredSplitRun(G4SplitRunChecks, unittest.TestCase):
    SITE = 'women-govt-nz-library'
