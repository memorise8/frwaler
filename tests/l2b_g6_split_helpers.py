"""Site glue for the L2b g6 enumeration splits (offline, memory-only).

Some g6 crawlers reach the network through a module-level ``_curl(url, ...)``
returning bytes, and their JSON listings end with an empty result list (not the
WordPress 400). These mixins drive the unchanged ``crawl()`` through that helper
with the fixture, and end synthetic runs with the fixture's ``empty_body``.
No network, process or database.
"""
import contextlib
import copy
import io
from unittest.mock import patch

import tests.collector_split_helpers as base
from crawler.enumeration.http import Response
from tests.l2b_g4_split_helpers import G4SplitChecks, G4SplitRunChecks


class ModuleCurlSplitChecks(G4SplitChecks):
    """crawl() calls module ``_curl(url, referer=..., retries=...) -> bytes``."""

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = base.load(self.SITE)
        captured, list_calls = [], []

        def curl(url, *args, **kwargs):
            if url in fixture['details']:
                return fixture['details'][url].encode()
            list_calls.append(url)
            return (fixture['listing_body'] if len(list_calls) == 1 else fixture['empty_body']).encode()

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', curl), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(list_calls), 2)
        return captured, list_calls


class EmptyBodyEndRunChecks(G4SplitRunChecks):
    """The page after the last one is the fixture's empty listing (200)."""

    def test_shipped_plan_completes_end_to_end(self):
        f = self.fixture
        end = lambda url: Response(200, {'Content-Type': f['content_type']}, f['empty_body'], url)
        result, docs = base.run_split(self.SITE, end=end)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))


class PlainNativeSplitChecks(G4SplitChecks):
    """crawl() reaches the network only through ``self._curl_get`` (no list-helper
    rerouting), with the g4 site-marker empty-page checks."""

    def native(self, fixture=None):
        return base.SplitChecks.native(self, fixture)
