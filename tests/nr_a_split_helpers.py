"""NR-20261018 group A: shared checks for enumeration splits (crawl() unchanged).
Memory-only: the original crawl() runs against a fake transport and its save argument is
compared with the framework adapter pipeline (tests.collector_split_helpers.SplitChecks).
No network, subprocess or database is used."""
import contextlib
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split


class NRSplitChecks(SplitChecks):
    # Names of the crawler's own fetch helpers (methods or module functions) the fake
    # transport replaces; the transport returns the fixture listing first, then empty_body.
    TRANSPORTS = ('_curl_get', '_curl', '_fetch', '_get')

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; list_calls = []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                value = fixture['details'][url]
                return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            list_calls.append(url)
            return fixture['listing_body'] if len(list_calls) == 1 else fixture['empty_body']

        crawler._delay = 0
        crawler._detail_delay = 0
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            for name in self.TRANSPORTS:
                if callable(getattr(type(crawler), name, None)):
                    stack.enter_context(patch.object(crawler, name, transport, create=True))
                if callable(getattr(module, name, None)):
                    stack.enter_context(patch.object(module, name, transport))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(list_calls), 2)
        return captured, calls

    def _parser(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        adapter = Adapter(self.SITE, p.section('adapter'), None, crawler=crawler)
        return p, ListingParser(p.listing['parser'], adapter=adapter)

    def test_added_listing_rejects_wrong_shape(self):
        p, _ = self._parser()
        c, _ = load(self.SITE)
        args = {'html': {'error': 'not a listing'}, 'json': {'error': 'not a listing'}, 'url': p.listing['url']}
        with self.assertRaises(PermanentFailure) as ctx:
            with guarded():
                c._enumeration_items(*[args[t] for t in p.section('adapter').get('list_args', ['html'])])
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'), ctx.exception.reason)

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, parser = self._parser()
        url = build_url(p.listing['url'], p.listing.get('params') or {})
        ctype = self.fixture['content_type']
        page = parser.parse(Response(200, {'Content-Type': ctype}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        try:
            err = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html><body>maintenance</body></html>', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertEqual([], err.items)
            self.assertTrue(err.extra.get('parse_error'))
            self.assertFalse(err.extra.get('explicit_empty', False))


class NRSplitRunChecks(SplitRunChecks):
    END_STATUS = 200  # what the page after the last one answers (200 empty listing or 404)

    def test_shipped_plan_completes_end_to_end(self):
        body = self.fixture['empty_body']
        status = self.END_STATUS
        ctype = self.fixture['content_type']
        result, docs = run_split(self.SITE, end=lambda url: Response(status, {'Content-Type': ctype}, body, url))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))
