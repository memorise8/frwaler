"""Authorized article scope: offline contract, unchanged thesis identity/date rules.

Reuse the no-network/no-database test boundary from the thesis suite; do not
inherit its tests or imply that the fake API's sort options exist on SONAR.
"""
import contextlib
import copy
import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.plan import load_plan, site_plan
from tests import test_enumeration_sonar_contract as thesis
from tests.sonar_memory import MemoryStore

SITE = thesis.SITE
PLAN = thesis.ROOT / 'tests/fixtures/sonar-articles-plan.json'


def article(n, day='2020-01-01', **updates):
    value = thesis.record(n, day, **updates)
    value['metadata']['documentType'] = 'coar:c_6501'
    return value


class ArticleAPI(thesis.API):
    scope = 'coar:c_6501'


class Articles(unittest.TestCase):
    setUp = thesis.Offline.setUp
    run_plan = thesis.Offline.run_plan

    def config(self, **over):
        data = copy.deepcopy(load_plan(PLAN))
        data['sites'][SITE]['segments'].update({'from': '2020', 'to': '2021'}, **over)
        return data

    def test_plan_validates_adapter_builds_and_scope_changes_fingerprint(self):
        data = load_plan(PLAN)
        plan = site_plan(data, SITE, registered=[SITE])
        old = site_plan(load_plan(thesis.PLAN), SITE, registered=[SITE])
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        self.assertFalse(adapter.needs_detail())
        self.assertTrue(adapter.identity_uses_list_fields())
        self.assertNotEqual(old.fingerprint, plan.fingerprint)
        self.assertEqual('coar:c_6501', plan.listing['params']['document_type'])
        self.assertEqual('coar:c_7a1f', old.listing['params']['document_type'])
        self.assertIs(True, plan.raw['overall_total_check'])
        self.assertEqual(9800, plan.section('segments')['hidden_cap'])
        self.assertIs(False, data['_review']['unique_sort_key_verified'])
        self.assertEqual(1.5, plan.get('request_interval_s'))

    def test_article_changes_only_category_and_preserves_input_identity_and_dates(self):
        for published in ('1999', '1999-02', '1999-02-03', ''):
            for pid in ('0000123', 123, None):
                with self.subTest(published=published, pid=pid):
                    rec = article(45, created='2020-01-01T23:59:59.999+01:00')
                    rec['metadata']['pid'] = pid
                    rec['metadata']['provisionActivity'][0]['startDate'] = published
                    before = copy.deepcopy(rec)
                    expected = self.crawler._parse_record(rec)
                    self.assertEqual('coar:c_7a1f', expected['category'])
                    expected['category'] = 'coar:c_6501'
                    actual = self.crawler._paper_from_article_result(rec)
                    self.assertEqual(expected, actual)
                    self.assertEqual(before, rec)
                    self.assertEqual(pid or '45', actual['post_number'])
                    self.assertEqual(pid or '45', actual['external_id'])
                    self.assertEqual(published, actual['published_date'])
                    self.assertEqual('2020-01-01', actual['posted_date'])
                    self.assertEqual(rec['created'], json.loads(actual['metadata'])['posted_date'])
                    self.assertEqual(paper_identity(clean_paper(expected)), paper_identity(clean_paper(actual)))
        rec = article(46)
        rec.pop('created')
        self.assertIsNone(self.crawler._paper_from_article_result(rec)['posted_date'])

    def test_wrong_missing_or_structured_type_is_rejected_in_list_and_paper(self):
        for kind in ('coar:c_7a1f', 'coar:c_6501 ', '', None, ['coar:c_6501'], {'value': 'coar:c_6501'}):
            with self.subTest(kind=kind):
                rec = article(1)
                rec['metadata']['documentType'] = kind
                with self.assertRaisesRegex(ValueError, 'article scope'):
                    self.crawler._parse_article_search_results({'hits': {'hits': [rec]}})
                with self.assertRaisesRegex(ValueError, 'article scope'):
                    self.crawler._paper_from_article_result(rec)
        rec = article(1)
        del rec['metadata']['documentType']
        with self.assertRaisesRegex(ValueError, 'article scope'):
            self.crawler._paper_from_article_result(rec)

    def test_unknown_shape_and_ambiguous_identity_remain_rejected(self):
        for value in ({}, [], {'hits': {'hits': 'unexpected'}}):
            with self.assertRaisesRegex(ValueError, 'article listing'):
                self.crawler._parse_article_search_results(value)
        self.assertEqual([], self.crawler._parse_article_search_results({'hits': {'hits': []}}))
        for value in (None, True, [], {}):
            rec = article(1)
            rec['metadata']['pid'] = value
            rec['id'] = value
            with self.assertRaisesRegex(ValueError, 'identity'):
                self.crawler._paper_from_article_result(rec)

    def test_existing_skip_rules_apply_to_articles(self):
        short = article(1)
        short['metadata']['abstracts'][0]['value'] = 'x' * 99
        no_title = article(2)
        no_title['metadata']['title'] = []
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIsNone(self.crawler._paper_from_article_result(short))
            self.assertIsNone(self.crawler._paper_from_article_result(no_title))
        short['metadata']['abstracts'][0]['value'] = 'x' * 100
        self.assertEqual('coar:c_6501', self.crawler._paper_from_article_result(short)['category'])

    def test_article_call_never_mutates_thesis_constant_or_paper_contract(self):
        original = self.crawler._paper_from_result(thesis.record(1))
        for _ in range(3):
            self.assertEqual('coar:c_6501', self.crawler._paper_from_article_result(article(2))['category'])
            self.assertEqual(original, self.crawler._paper_from_result(thesis.record(1)))
            self.assertEqual('coar:c_7a1f', type(self.crawler).crawl.__globals__['_DOC_TYPE'])

    def test_articles_split_and_reconcile_with_article_papers(self):
        records = [article(i, '2020-01-%02d' % (i % 28 + 1)) for i in range(125)]
        api = ArticleAPI(records)
        result = self.run_plan(api, self.config(hidden_cap=60))
        self.assertEqual('complete', result['status'], result)
        segments = {r['segment_key']: r for r in self.store.data['segments'].values()}
        self.assertEqual('split', segments['2020']['status'])
        self.assertEqual('split', segments['2020-01']['status'])
        self.assertEqual(125, sum(r['reported_total'] for r in segments.values() if r['status'] != 'split'))
        self.assertEqual(125, self.store.site(SITE)['state']['overall_total'])
        self.assertEqual(125, len(self.docs))
        for rec in records:
            self.assertEqual(clean_paper(self.crawler._paper_from_article_result(rec)), self.docs[rec['metadata']['pid']])
        self.assertEqual({'/robots.txt', '/api/documents/'}, {urlsplit(u).path for u in api.urls})

    def test_article_budget_serialized_resume(self):
        records = [article(i) for i in range(125)]
        first = self.run_plan(ArticleAPI(records), max_requests=3)
        self.assertEqual(('incomplete', 'request_budget'), (first['status'], first['stop_reason']))
        self.assertTrue(0 < len(self.docs) < 125)
        restored = json.loads(json.dumps({'state': self.store.data, 'docs': self.docs}))
        self.store = MemoryStore()
        self.store.data = restored['state']
        self.docs = restored['docs']
        result = self.run_plan(ArticleAPI(records))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(125, len(self.docs))
        self.assertEqual({'coar:c_6501'}, {p['category'] for p in self.docs.values()})

    def test_scope_leak_never_saves_a_thesis_as_article(self):
        api = ArticleAPI([article(1), thesis.record(2)])
        with self.assertRaisesRegex(ValueError, 'article scope'):
            self.run_plan(api)
        self.assertEqual('failed', self.store.site(SITE)['status'])
        self.assertEqual('runner_error:ValueError', self.store.site(SITE)['stop_reason'])
        self.assertEqual({}, self.docs)
        self.assertEqual(2, len(api.urls))  # robots and first listing, no further request

    def test_broken_listing_with_zero_total_is_not_an_empty_source(self):
        class BrokenAPI(ArticleAPI):
            def __call__(self, url):
                response = super().__call__(url)
                if '/api/documents/' in url:
                    # A source/schema failure, not the explicit valid hits.hits=[] shape.
                    return Response(200, {'Content-Type': 'application/json'},
                                    json.dumps({'hits': {'total': {'value': 0, 'relation': 'eq'}}}), url)
                return response
        with self.assertRaisesRegex(ValueError, 'article listing'):
            self.run_plan(BrokenAPI([]))
        self.assertEqual('failed', self.store.site(SITE)['status'])
        self.assertEqual({}, self.docs)

    def test_missing_created_is_caught_by_total_reconciliation(self):
        result = self.run_plan(ArticleAPI([article(1), article(2, created='')]))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])

    def test_daily_cap_is_not_collection_completion(self):
        result = self.run_plan(ArticleAPI([article(i) for i in range(80)]), self.config(hidden_cap=30))
        self.assertNotEqual('complete', result['status'], result)
        self.assertTrue(any(r['status'] == 'cap_unsplittable' for r in self.store.data['segments'].values()))

    def test_article_date_ties_652_to_616_are_detected(self):
        class UnstableAPI(ArticleAPI):
            def __call__(self, url):
                response = super().__call__(url)
                if '/api/documents/' not in url:
                    return response
                body = response.json()
                if parse_qs(urlsplit(url).query).get('page') == ['2'] and len(body['hits']['hits']) == 50:
                    ordered = sorted(self.records, key=lambda r: (r['created'], str(r['id'])))
                    body['hits']['hits'][:36] = copy.deepcopy(ordered[:36])
                return Response(200, {'Content-Type': 'application/json'}, json.dumps(body), url)
        result = self.run_plan(UnstableAPI([article(i) for i in range(652)]))
        self.assertEqual(616, len(self.docs))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        day = next(r for r in self.store.data['segments'].values() if r['segment_key'] == '2020-01-01')
        self.assertEqual((616, 652, 'mismatch'), (day['listed'], day['reported_total'], day['status']))
        self.assertIn('overall_total', result['stop_reason'])

    def test_article_stable_synthetic_tie_break_gets_all_652(self):
        result = self.run_plan(ArticleAPI([article(i) for i in range(652)]))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(652, len(self.docs))
        self.assertEqual(0, result['duplicates']['new'])
