"""Second-batch synthetic comparisons; original crawl executes with memory transports.
No network/process/customer DB. Runner-only tests use disposable SQLite fixtures.
"""
import contextlib
import copy
import io
import json
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split, wp_past_last_page


class Split2Checks(SplitChecks):
    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                value = fixture['details'][url]
                return json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value
            listing_calls.append(url)
            if len(listing_calls) == 1 or fixture.get('repeat_first_listing') and len(listing_calls) == 2:
                return fixture['listing_body']
            return fixture['empty_body']
        crawler._delay = 0
        crawler._detail_delay = 0
        crawler.detail_sleep = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get = transport
        def json_response(url, **kwargs):
            body = transport(url)
            return SimpleNamespace(status_code=200, headers={'X-WP-TotalPages':'1'},
                                   json=lambda: json.loads(body), raise_for_status=lambda: None)
        crawler._session = SimpleNamespace(get=json_response)
        with contextlib.ExitStack() as stack:
            if hasattr(module, '_curl_get'):
                stack.enter_context(patch.object(module, '_curl_get', transport))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        # This source constructs its abstract from metadata including the publisher;
        # shortening topics does NOT legitimately make it a short-abstract item.
        self.assertEqual(2 if f.get('short_keeps_items') else 0, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))
        self.assertEqual(expected, self.raw_papers)

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(self.SITE)
        args = [{'error':'not a listing'}]
        if self.SITE in ['education-ni-gov-uk-news','culture-gouv-fr-espace-documentation']:
            args.append(self.plan['sites'][self.SITE]['listing']['start_page'])
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(*args)
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        from crawler.enumeration.urls import build_url
        url = build_url(p.listing['url'], p.listing.get('params'))
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE,p.section('adapter'),None,crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type':self.fixture['content_type']},self.fixture['empty_body'],url))
        self.assertEqual([],page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200,{'Content-Type':self.fixture['content_type']},
                                  '{"error":"maintenance"}' if self.fixture['content_type']=='application/json' else '<html>maintenance</html>',url))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class Split2RunChecks(SplitRunChecks):
    def test_shipped_plan_completes_end_to_end(self):
        def end(url):
            return wp_past_last_page(url) if self.SITE=='kit-nl-institute' else Response(
                200,{'Content-Type':self.fixture['content_type']},self.fixture['empty_body'],url)
        result, docs = run_split(self.SITE,end=end,total=2 if self.SITE=='kit-nl-institute' else None)
        self.assertEqual('complete',result['status'],result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'),result)
        self.assertEqual(2,len(docs))
        self.assertEqual(2,len({d['post_number'] for d in docs}))
        self.assertEqual(2,len({d['meta_url'] for d in docs}))

    def test_short_paper_does_not_silently_become_complete(self):
        fixture=json.loads(json.dumps(self.fixture,ensure_ascii=False).replace(self.fixture['long_text'],'Short.'))
        def end(url):
            return wp_past_last_page(url) if self.SITE=='kit-nl-institute' else Response(
                200,{'Content-Type':fixture['content_type']},fixture['empty_body'],url)
        result,docs=run_split(self.SITE,fixture=fixture,end=end,total=2 if self.SITE=='kit-nl-institute' else None)
        self.assertEqual('complete' if fixture.get('short_keeps_items') else 'complete_with_review',result['status'],result)
        self.assertEqual(2 if fixture.get('short_keeps_items') else 0,len(docs))
