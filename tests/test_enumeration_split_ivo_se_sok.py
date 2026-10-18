"""ivo-se-sok: server-rendered /sok/ listing (?p=N) + pure detail conversion (memory only).

The framework sends plain GET requests, so the listing is the HTML search page, which
crawl() itself handles through its non-JSON fallback (search meta ``{}``). The original
crawl() is driven with that same HTML through its own network method ``_curl_text``.
"""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'ivo-se-sok'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, list_calls = [], [], []
        def transport(url, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return fixture['details'][url]
            list_calls.append(url)
            return fixture['listing_body'] if len(list_calls) == 1 else fixture['empty_body']
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_text = transport
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(2, len(list_calls))
        return captured, calls

    def test_identity_rule_is_kept(self):
        papers = self.pipeline()
        # post_number = Publikationsnummer, else numeric token of Diarienummer (crawl() rule).
        self.assertEqual(['IVO 2026-1', '3.5.1-12342/2026'], [p['post_number'] for p in papers][:1] + [p['external_id'] for p in papers][1:])
        self.assertEqual(2, len({paper_identity(p) for p in papers}))
        self.assertEqual('{}', json.dumps(json.loads(papers[0]['metadata'])['search_meta']))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        url = build_url(p.listing['url'], p.listing.get('params'))
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        try:
            error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        except PermanentFailure as exc:
            self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
        else:
            self.assertTrue(error_page.extra.get('parse_error'))
            self.assertFalse(error_page.extra.get('explicit_empty', False))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def end(self, fixture):
        return lambda url: Response(200, {'Content-Type': 'text/html'}, fixture['empty_body'], url)

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split(self.SITE, end=self.end(self.fixture))
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs = run_split(self.SITE, fixture=fixture, end=self.end(fixture))
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)
