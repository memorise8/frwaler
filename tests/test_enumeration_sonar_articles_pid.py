"""Article scope split by pid range bisection: synthetic wire, no network or database.

Reuses the thesis/article offline harness; the date plan fixture stays untouched and
its _created split is never exercised here.
"""
import copy
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper
from crawler.enumeration.http import Response
from crawler.enumeration.plan import load_plan, site_plan
from tests import test_enumeration_sonar_articles as articles
from tests import test_enumeration_sonar_contract as thesis
from tests.sonar_memory import MemoryStore

SITE = thesis.SITE
PID_PLAN = thesis.ROOT / 'tests/fixtures/sonar-articles-plan-pid.json'
DATE_PLAN = articles.PLAN


def article(pid, **updates):
    rec = articles.article(pid, **updates)
    rec['metadata']['pid'] = str(pid)
    rec['id'] = 'id-%d' % pid
    return rec


class PidAPI:
    scope = 'coar:c_6501'

    def __init__(self, records, *, hide=(), overall_delta=0):
        self.records = records
        self.hide = set(hide)
        self.overall_delta = overall_delta
        self.urls = []
        self.max_window = 0

    def queries(self):
        return [parse_qs(urlsplit(u).query).get('q', [''])[0] for u in self.urls if '/api/documents/' in u]

    def __call__(self, url):
        self.urls.append(url)
        u = urlsplit(url)
        assert u.hostname == 'sonar.ch'
        if u.path == '/robots.txt':
            return Response(200, {}, b'User-agent: *\nAllow: /\n', url)
        assert u.path == '/api/documents/', 'detail/file requests forbidden'
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        assert q['document_type'] == self.scope and q['view'] == 'global' and q['sort'] == 'oldest'
        size, page = int(q['size']), int(q['page'])
        assert size == 50
        self.max_window = max(self.max_window, page * size)
        rows = self.records
        if q.get('q'):
            lo, hi = map(int, q['q'][len('pid:['):-1].split(' TO '))   # raises on any other shape
            assert q['q'] == 'pid:[%d TO %d]' % (lo, hi)
            rows = [r for r in rows if lo <= int(r['metadata']['pid']) <= hi]
        else:
            rows = [r for r in rows]
        rows = sorted(rows, key=lambda r: int(r['metadata']['pid']))
        total = len(rows) + (0 if q.get('q') else self.overall_delta)
        served = [r for r in rows if r['metadata']['pid'] not in self.hide]
        body = {'hits': {'total': {'value': total, 'relation': 'eq'},
                         'hits': copy.deepcopy(served[(page - 1) * size: page * size])}}
        return Response(200, {'Content-Type': 'application/json'}, json.dumps(body), url)


class ArticlePidSplit(unittest.TestCase):
    setUp = thesis.Offline.setUp
    run_plan = thesis.Offline.run_plan

    def config(self, **over):
        data = copy.deepcopy(load_plan(PID_PLAN))
        data['sites'][SITE]['segments'].update(over)
        return data

    def leaves(self):
        return {r['segment_key']: r for r in self.store.data['segments'].values() if r['status'] != 'split'}

    def test_plan_is_pid_bisection_and_keeps_the_article_contract(self):
        data = load_plan(PID_PLAN)
        plan = site_plan(data, SITE, registered=[SITE])
        date_plan = site_plan(load_plan(DATE_PLAN), SITE, registered=[SITE])
        thesis_plan = site_plan(load_plan(thesis.PLAN), SITE, registered=[SITE])
        self.assertEqual({'type': 'query_int_range', 'param': 'q', 'field': 'pid'}, plan.section('segments')['filter'])
        self.assertEqual((0, 9999999, 9800), tuple(plan.section('segments')[k] for k in ('lo', 'hi', 'hidden_cap')))
        self.assertEqual('coar:c_6501', plan.listing['params']['document_type'])
        self.assertEqual((50, 'oldest', 'global'), tuple(plan.listing['params'][k] for k in ('size', 'sort', 'view')))
        self.assertEqual(date_plan.listing, plan.listing)
        self.assertEqual(date_plan.section('adapter'), plan.section('adapter'))
        self.assertIs(True, plan.raw['overall_total_check'])
        self.assertEqual(3, len({plan.fingerprint, date_plan.fingerprint, thesis_plan.fingerprint}))
        self.assertEqual('_created', date_plan.section('segments')['filter']['field'])   # date plan preserved
        self.assertEqual('coar:c_7a1f', thesis_plan.listing['params']['document_type'])  # thesis plan preserved
        adapter = Adapter(SITE, plan.section('adapter'), None, crawler=self.crawler)
        self.assertFalse(adapter.needs_detail())
        self.assertIs(False, data['_review']['pid_range_numeric_semantics_verified'])

    def test_bisection_reconciles_with_the_same_article_papers(self):
        pids = list(range(1, 200, 2)) + list(range(5000, 5060)) + [9000, 9001]
        records = [article(p) for p in pids]
        api = PidAPI(records)
        result = self.run_plan(api, self.config(hi=9999, hidden_cap=60))
        self.assertEqual('complete', result['status'], result)
        leaves = self.leaves()
        self.assertTrue(all(r['status'] == 'complete' and r['evidence'] == 'total_matched' for r in leaves.values()))
        self.assertTrue(all(r['reported_total'] <= 60 for r in leaves.values()))
        self.assertEqual(len(records), sum(r['reported_total'] for r in leaves.values()))
        self.assertEqual(len(records), self.store.site(SITE)['state']['overall_total'])
        self.assertEqual(len(records), len(self.docs))
        for rec in records:
            self.assertEqual(clean_paper(self.crawler._paper_from_article_result(rec)), self.docs[rec['metadata']['pid']])
        self.assertEqual({'coar:c_6501'}, {p['category'] for p in self.docs.values()})
        self.assertEqual({'/robots.txt', '/api/documents/'}, {urlsplit(u).path for u in api.urls})
        self.assertEqual('pid:0-9999', next(iter(sorted(
            (r['segment_key'] for r in self.store.data['segments'].values() if r['parent_key'] is None)))))

    def test_default_cap_keeps_every_request_inside_the_10000_window(self):
        records = [article(p) for p in range(20001)]
        api = PidAPI(records)
        result = self.run_plan(api)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(20001, len(self.docs))
        # A 9800-record leaf is 196 pages; the end check reads up to page 198 (window 9900 < 10000).
        self.assertLessEqual(api.max_window, 9900)
        self.assertEqual('pid:[0 TO 9999999]', api.queries()[0])
        self.assertTrue(all(r['reported_total'] <= 9800 for r in self.leaves().values()))

    def test_empty_high_range_completes_without_reading_pages(self):
        records = [article(p) for p in range(1, 90)]
        api = PidAPI(records)
        result = self.run_plan(api, self.config(hidden_cap=60))
        self.assertEqual('complete', result['status'], result)
        empty = [r for r in self.leaves().values() if r['reported_total'] == 0]
        self.assertTrue(empty)
        self.assertTrue(all(r['status'] == 'complete' and r['evidence'] == 'total_matched' for r in empty))
        for key in (k for k, r in self.leaves().items() if r['reported_total'] == 0):
            lo, hi = key.split(':')[1].split('-')
            asked = [u for u in api.urls if parse_qs(urlsplit(u).query).get('q') == ['pid:[%s TO %s]' % (lo, hi)]]
            self.assertEqual(['1', '1'], [parse_qs(urlsplit(u).query)['page'][0] for u in asked], key)

    def test_one_pid_over_cap_is_never_complete(self):
        records = [article(p) for p in range(1, 10)]
        for i in range(6):          # SONAR pids are unique; this only exercises the guard
            rec = article(50)
            rec['id'] = 'dup-%d' % i
            records.append(rec)
        result = self.run_plan(PidAPI(records), self.config(hidden_cap=4))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertEqual('cap_unsplittable', self.leaves()['pid:50-50']['status'])
        self.assertIn('pid:50-50', result['stop_reason'])

    def test_resume_gets_the_same_ranges_and_all_papers(self):
        records = [article(p) for p in range(1, 400, 2)]
        whole = PidAPI(records)
        self.run_plan(whole, self.config(hidden_cap=40))
        reference = set(self.store.data['segments'])
        self.store, self.docs = MemoryStore(), {}
        first = self.run_plan(PidAPI(records), self.config(hidden_cap=40), max_requests=5)
        self.assertEqual(('incomplete', 'request_budget'), (first['status'], first['stop_reason']))
        restored = json.loads(json.dumps({'state': self.store.data, 'docs': self.docs}))
        self.store = MemoryStore()
        self.store.data, self.docs = restored['state'], restored['docs']
        result = self.run_plan(PidAPI(records), self.config(hidden_cap=40))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(reference, set(self.store.data['segments']))
        self.assertEqual(len(records), len(self.docs))
        self.assertEqual(0, result['duplicates']['new'])

    def test_unserved_record_or_wrong_overall_total_is_not_completion(self):
        records = [article(p) for p in range(1, 60)]
        result = self.run_plan(PidAPI(records, hide=['17']), self.config(hidden_cap=20))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        bad = self.leaves()['pid:17-17']
        self.assertEqual(('mismatch', 1, 0), (bad['status'], bad['reported_total'], bad['listed']))
        self.assertIn('overall_total', result['stop_reason'])
        self.store, self.docs = MemoryStore(), {}
        result = self.run_plan(PidAPI(records, overall_delta=2), self.config(hidden_cap=20))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])

    def test_scope_leak_still_stops_before_saving(self):
        records = [article(1), thesis.record(2)]
        records[1]['metadata']['pid'] = '2'
        with self.assertRaisesRegex(ValueError, 'article scope'):
            self.run_plan(PidAPI(records), self.config())
        self.assertEqual({}, self.docs)


if __name__ == '__main__':
    unittest.main()
