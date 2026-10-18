"""arn-se-om-arn: the single årsredovisning page (no pagination; crawl() reads one page).
Page numbers above 1 are declared empty by the list method and proved by the
listing_marker; rows become papers with the pure copy of crawl()'s conversion."""
import copy
import json
import unittest

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from crawler.enumeration.urls import build_url
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, run_split

SITE = 'arn-se-om-arn'
MAINTENANCE = '<html><body><p>Site under maintenance</p></body></html>'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_full_paper_and_identity_equal_original_crawl(self):
        super().test_full_paper_and_identity_equal_original_crawl()
        self.assertEqual(['2031', '2030'], [p['post_number'] for p in self.raw_papers])

    def test_short_abstract_skip_is_preserved(self):
        # The abstract is built from a template around the preamble: a short preamble
        # never makes an item short, in crawl() or in the adapter.
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual(2, len(expected))
        self.assertEqual([clean_paper(p) for p in expected], self.pipeline(f))

    def _parser(self):
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        return p, ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p, parser = self._parser()
        page2 = build_url(p.listing['url'], {'page': 2})
        # The real site answers ?page=2 with the same single page: declared empty, marker present.
        empty = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], page2))
        self.assertEqual([], empty.items)
        self.assertTrue(empty.extra.get('explicit_empty'))
        # A foreign page past the start is not an end.
        foreign = parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, page2))
        self.assertEqual('listing_structure_missing', foreign.extra.get('parse_error'))
        self.assertFalse(foreign.extra.get('explicit_empty', False))
        # The start page without PDF links is an error (crawl() reports invalid_response).
        for url in (p.listing['url'], build_url(p.listing['url'], {'page': 1})):
            with self.assertRaises(PermanentFailure) as ctx:
                parser.parse(Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url))
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_repeated_reporting_year_is_a_listing_error(self):
        crawler, _ = load(self.SITE)
        body = self.fixture['listing_body'].replace('synthetic-arsredovisning-2031.pdf', 'other-2030.pdf')
        with self.assertRaises(PermanentFailure) as ctx:
            crawler._enumeration_items(body, 'https://www.arn.se/om-arn/arsredovisning/')
        self.assertEqual('listing_parse_error:reporting_year_not_unique', ctx.exception.reason)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        same_page = lambda url: Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], url)
        result, docs = run_split(self.SITE, end=same_page)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'2031', '2030'}, {d['post_number'] for d in docs})

    def test_foreign_page_after_start_does_not_complete(self):
        foreign = lambda url: Response(200, {'Content-Type': 'text/html'}, MAINTENANCE, url)
        result, docs = run_split(self.SITE, end=foreign)
        self.assertNotEqual('complete', result['status'], result)
