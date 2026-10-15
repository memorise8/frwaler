"""Explicit verified partition trees. All transport is synthetic, no network."""
import copy
import json
import unittest
from datetime import date
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.plan import PlanError, site_plan
from tests.enumeration_fakes import Env
from tests.test_enumeration_ntrs_contract import crawler_class, record


def plan(cap=5):
    return {
        'strategy': 'date_range_split', 'request_interval_s': 0,
        'adapter': {'type': 'list_only'}, 'overall_total_check': True,
        'listing': {'url': 'https://tree.test/api', 'page_param': 'page', 'start_page': 1,
                    'params': {'size': 3},
                    'parser': {'type': 'json', 'items_path': 'items', 'key': 'id',
                               'url_template': 'https://tree.test/doc/{id}', 'title': 'title'},
                    'total': {'type': 'json', 'path': 'total'}},
        'segments': {'mode': 'partition_tree', 'server_verified': True, 'root': 'all',
                     'hidden_cap': cap, 'count_size_param': 'size',
                     'nodes': [{'key': 'all', 'params': {}, 'children': ['day', 'residual']},
                               {'key': 'day', 'params': {'bucket': 'day'}, 'children': ['a', 'b']},
                               {'key': 'a', 'params': {'bucket': 'day', 'kind': 'a'}},
                               {'key': 'b', 'params': {'bucket': 'day', 'kind': 'b'}},
                               {'key': 'residual', 'params': {'bucket': 'residual'}}]}}


class API:
    def __init__(self, n=8, *, delta=0, overlap=False, drift=False):
        self.rows = [{'id': str(i), 'title': 't', 'bucket': 'day' if i < n else 'residual',
                      'kind': 'a' if i % 2 else 'b',
                      'created': '2026-10-12' if i < n else (None if i == n else '0202-08-01')}
                     for i in range(n + 2)]
        self.delta, self.overlap, self.drift = delta, overlap, drift
        self.urls = []

    def __call__(self, url):
        self.urls.append(url)
        u = urlsplit(url)
        if u.path == '/robots.txt':
            return Response(404, {}, b'')
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        rows = [r for r in self.rows if all(r.get(k) == q[k] for k in ('bucket', 'kind') if k in q)]
        if self.overlap and q.get('kind') == 'b':
            rows = [dict(rows[0], id='1')] + rows[1:]
        total = len(rows) + (self.delta if q.get('kind') == 'a' else 0)
        if self.drift and q.get('kind') == 'a' and q['size'] != '1':
            total += 1
        page, size = int(q['page']), int(q['size'])
        return Response(200, {'Content-Type': 'application/json'}, json.dumps({
            'total': total, 'items': rows[(page - 1) * size:page * size]}), url)


class PartitionTreeTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.addCleanup(self.env.close)

    def test_date_then_category_including_missing_and_ancient_created(self):
        result = self.env.run(plan(), API())
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(10, len(self.env.docs()))
        self.assertEqual(10, len({r['post_number'] for r in self.env.docs()}))

    def test_count_mismatch_stops_before_any_parent_items_saved(self):
        result = self.env.run(plan(), API(delta=1))
        self.assertEqual('incomplete', result['status'], result)
        self.assertIn('partition_child_total_mismatch', result['stop_reason'])
        self.assertEqual([], self.env.docs())

    def test_overlap_even_with_balanced_totals_is_not_complete(self):
        result = self.env.run(plan(), API(overlap=True))
        self.assertNotEqual('complete', result['status'], result)

    def test_count_changes_before_enumeration_stops(self):
        result = self.env.run(plan(), API(drift=True))
        self.assertEqual('incomplete', result['status'], result)
        self.assertIn('partition_count_changed', result['stop_reason'])

    def test_unsplittable_equal_cap_stops(self):
        p = plan()
        p['segments']['nodes'][1].pop('children')
        p['segments']['nodes'] = [n for n in p['segments']['nodes'] if n['key'] not in ('a', 'b')]
        result = self.env.run(p, API())
        self.assertEqual('incomplete', result['status'], result)
        self.assertIn('partition_cap_unsplittable', result['stop_reason'])

    def test_new_fingerprint_reuses_document_db_without_duplicates(self):
        from crawler.enumeration.store import Store
        p = plan(cap=6)
        self.assertEqual('complete', self.env.run(p, API())['status'])
        old_store = self.env.store
        self.addCleanup(old_store.close)
        self.env.store = Store(self.env.root / 'second-pass.sqlite')
        q = copy.deepcopy(p)
        q['segments']['nodes'][0]['key'] = q['segments']['root'] = 'whole'
        self.assertNotEqual(self.env.plan(p).fingerprint, self.env.plan(q).fingerprint)
        result = self.env.run(q, API(9))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(11, len(self.env.docs()))
        self.assertEqual(11, len({r['post_number'] for r in self.env.docs()}))

    def test_budget_resume(self):
        p = plan()
        self.assertNotEqual('complete', self.env.run(p, API(), max_requests=8)['status'])
        result = self.env.run(p, API())
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(10, len(self.env.docs()))

    def test_39954_migration_day_splits_without_walking_capped_parent(self):
        # Enumerate every synthetic ID in the real strategy/store without 40k
        # document-save transactions. Smaller end-to-end DB saves are above.
        p = plan(cap=9800)
        nodes = p['segments']['nodes']
        nodes[1]['children'] = [f'part{i}' for i in range(5)]
        p['segments']['nodes'] = nodes[:2] + [nodes[-1]] + [
            {'key': f'part{i}', 'params': {'bucket': 'day', 'kind': str(i)}} for i in range(5)]
        api = API(39954)
        for i, row in enumerate(api.rows[:39954]):
            row['kind'] = str(i % 5)
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.runner import Context
        from crawler.enumeration.strategies import DateRangeSplit
        from tests.enumeration_fakes import FakeTransport
        p['listing']['params']['size'] = 1000
        sp = self.env.plan(p)
        adapter = Adapter(sp.site_id, p['adapter'], self.env.conn)
        self.addCleanup(adapter.close)
        state = self.env.store.start_site(sp.site_id, sp.fingerprint, sp.strategy)
        parser = ListingParser(sp.listing['parser'], total_cfg=sp.listing['total'], adapter=adapter)
        ctx = Context(sp.site_id, sp, sp.fingerprint, self.env.store,
                      self.env.fetcher(sp, FakeTransport(api)), parser, date(2026, 10, 12), state)
        strategy = DateRangeSplit(ctx)
        for batch in strategy.batches():
            self.env.store.mark_listed(sp.site_id, sp.fingerprint, batch.segment, batch.listed_keys)
            self.env.store.update_segment(sp.site_id, sp.fingerprint, batch.segment, cursor=batch.cursor,
                                          reported_total=batch.reported_total)
        self.assertEqual(('complete', 'all_segments_reconciled'), strategy.outcome)
        self.assertEqual(39956, self.env.store.listed_count(sp.site_id, sp.fingerprint))
        segments = self.env.store.segments(sp.site_id, sp.fingerprint)
        day = next(r for r in segments if r['segment_key'] == 'day')
        self.assertEqual(39954, day['reported_total'])
        self.assertEqual('split', day['status'])
        self.assertEqual(0, day['listed'])
        self.assertFalse(any('bucket=day' in u and 'kind=' not in u and 'size=1000' in u for u in api.urls))

    def test_rejects_unverified_cycle_shared_nodes_and_disabled_checks(self):
        for mutate in (
            lambda p: p['segments'].update(server_verified=False),
            lambda p: p.update(overall_total_check=False),
            lambda p: p['segments'].update(allow_overlap=True),
            lambda p: p['segments']['nodes'][1].update(children=['all', 'a']),
            lambda p: p['segments']['nodes'][2].update(params={'size': 1}),
        ):
            p = plan()
            mutate(p)
            with self.subTest(p=p), self.assertRaises(PlanError):
                self.env.plan(p)

    def test_raw_date_provenance_preserves_native_identity_and_selection(self):
        crawler = crawler_class()(self.env.conn)
        for dated in (True, False):
            item = record(1, date(2026, 10, 12), dated=dated)
            item['created'] = '0202-08-01'
            native = crawler._paper_from_result(item)
            audited = crawler._paper_from_result_with_source_dates(item)
            self.assertEqual(native['post_number'], audited['post_number'])
            from crawler.document_dates import normalize_date
            self.assertEqual(native['published_date'], normalize_date(audited['published_date'])['value'])
            raw = audited['metadata']['source_dates']
            self.assertEqual('0202-08-01', raw['created'])
            self.assertEqual(item['submittedDate'], raw['submittedDate'])
            self.assertEqual('unconfirmed', raw['submittedDate_publication_semantics'])
        item['submittedDate'] = None
        item['created'] = '2026-10-12'
        self.assertIsNone(crawler._paper_from_result_with_source_dates(item)['published_date'])

    def test_native_contract_stores_undated_with_raw_provenance(self):
        from crawler.enumeration.store import Store
        site = 'ntrs-nasa-gov-search'
        env = Env(site)
        self.addCleanup(env.close)
        p = plan()
        p['adapter'] = {'type': 'crawler', 'contract_version': 1,
                        'list_method': '_parse_search_results', 'list_args': ['json'],
                        'paper_method': '_paper_from_result_with_source_dates',
                        'paper_args': [{'source': 'item', 'path': ['result']}]}
        p['listing']['parser'] = {'type': 'adapter', 'key_field': 'id', 'url_field': 'url',
                                  'date_field': 'published_date', 'title_field': 'title'}
        class NativeAPI(API):
            def __call__(self, url):
                response = super().__call__(url)
                if '/robots.txt' not in url:
                    data = response.json()
                    data['results'] = data.pop('items')
                    return Response(200, {'Content-Type': 'application/json'}, json.dumps(data), url)
                return response
        api = NativeAPI()
        for i, row in enumerate(api.rows):
            native = record(i, date(2026, 10, 12), dated=False)
            native.update({k: row[k] for k in ('bucket', 'kind', 'created')})
            api.rows[i] = native
        result = env.run(p, api, crawler=crawler_class()(env.conn))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(10, len(env.docs()))
        stored = env.conn.execute('SELECT published_date_raw, published_date_status FROM documents LIMIT 1').fetchone()
        self.assertEqual(api.rows[0]['submittedDate'], stored[0])
        self.assertEqual('normalized', stored[1])
        self.assertEqual('2026-10-12', env.docs()[0]['published_date'])
        # Repeat with a fresh state and the same document DB, preserving native IDs.
        old = env.store
        self.addCleanup(old.close)
        env.store = Store(env.root / 'pass2.sqlite')
        self.assertEqual('complete', env.run(p, api, crawler=crawler_class()(env.conn))['status'])
        self.assertEqual(10, len(env.docs()))

    def test_unknown_count_is_not_completion(self):
        api = API()
        def handler(url):
            response = api(url)
            if '/robots.txt' in url:
                return response
            data = response.json()
            data.pop('total')
            return Response(200, {'Content-Type': 'application/json'}, json.dumps(data), url)
        result = self.env.run(plan(), handler)
        self.assertEqual('incomplete', result['status'], result)
        self.assertEqual([], self.env.docs())

    def test_global_snapshot_mismatch_cannot_be_complete(self):
        api = API()
        def handler(url):
            response = api(url)
            if '/robots.txt' in url:
                return response
            q = parse_qs(urlsplit(url).query)
            if 'bucket' not in q and q['size'] == ['3']:
                data = response.json()
                data['total'] += 1
                return Response(200, {'Content-Type': 'application/json'}, json.dumps(data), url)
            return response
        result = self.env.run(plan(), handler)
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])

    def test_partial_and_invalid_selected_dates_not_fabricated(self):
        from crawler.document_dates import normalize_document_dates
        crawler = crawler_class()(self.env.conn)
        for text, status in [('2025', 'partial'), ('2025-02-30', 'invalid')]:
            item = record(1, date(2026, 10, 12), dated=False)
            item['submittedDate'] = text
            paper = crawler._paper_from_result_with_source_dates(item)
            result = normalize_document_dates(paper)
            self.assertIsNone(result['published_date'])
            self.assertEqual(text, result['published_date_raw'])
            self.assertEqual(status, result['published_date_status'])
