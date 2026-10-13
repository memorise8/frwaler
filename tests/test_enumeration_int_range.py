"""Integer id-range bisection (query_int_range) against a fake range-query API. No network."""
import copy
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.plan import PlanError, site_plan
from crawler.enumeration.strategies import _int_children, generate_segments
from tests.enumeration_fakes import Env, FakeTransport

PAGE = 10


def plan(lo=0, hi=1023, cap=25, **over):
    site = {
        'strategy': 'date_range_split', 'request_interval_s': 0, 'adapter': {'type': 'list_only'},
        'listing': {'url': 'https://pid.test/api', 'params': {'scope': 'x', 'size': PAGE}, 'page_param': 'page',
                    'start_page': 1,
                    'parser': {'type': 'json', 'items_path': 'hits.hits', 'key': 'id',
                               'url_template': 'https://pid.test/doc/{id}', 'title': 'title'},
                    'total': {'type': 'json', 'path': 'hits.total.value'}},
        'segments': {'mode': 'range', 'lo': lo, 'hi': hi, 'hidden_cap': cap,
                     'filter': {'type': 'query_int_range', 'param': 'q', 'field': 'pid'}},
        'overall_total_check': True}
    site.update(over)
    return site


class API:
    """pid:[a TO b] over numeric pids, sorted by pid, PAGE per page. ``pids`` may repeat a value."""

    def __init__(self, pids, *, hide=(), overall_delta=0):
        self.docs = [{'id': 'd%d' % i, 'pid': p, 'title': 't'} for i, p in enumerate(pids)]
        self.hide = set(hide)       # pids that are counted in every total but never served as hits
        self.overall_delta = overall_delta
        self.urls = []

    def ranges(self):
        return [parse_qs(urlsplit(u).query).get('q', [''])[0] for u in self.urls if '/api' in u]

    def __call__(self, url):
        self.urls.append(url)
        u = urlsplit(url)
        if u.path == '/robots.txt':
            return Response(404, {}, b'')
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        rows = self.docs
        served = False
        if q.get('q'):
            lo, hi = map(int, q['q'][len('pid:['):-1].split(' TO '))
            rows = [d for d in rows if lo <= d['pid'] <= hi]
            served = True
        rows = sorted(rows, key=lambda d: (d['pid'], d['id']))
        total = len(rows) + (0 if served else self.overall_delta)
        rows_served = [d for d in rows if d['pid'] not in self.hide]
        page = int(q['page'])
        body = {'hits': {'total': {'value': total, 'relation': 'eq'}, 'hits': rows_served[(page - 1) * PAGE: page * PAGE]}}
        return Response(200, {'Content-Type': 'application/json'}, json.dumps(body), url)


def reference_leaves(total, key=(0, 1023), cap=25):
    """Independent model of the rule: leaf when total <= cap or lo == hi, else halves at mid / mid+1."""
    lo, hi = key
    if total(lo, hi) <= cap or lo == hi:
        return [(lo, hi)]
    mid = (lo + hi) // 2
    return reference_leaves(total, (lo, mid), cap) + reference_leaves(total, (mid + 1, hi), cap)


class IntRange(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def segments(self):
        site = self.env.store.site(self.env.site_id)
        return {s['segment_key']: s for s in self.env.store.segments(self.env.site_id, site['plan_fp'])}

    def leaves(self):
        return {k: s for k, s in self.segments().items() if s['status'] != 'split'}

    def test_keys_are_deterministic_disjoint_and_cover_the_range(self):
        self.assertEqual(['pid:0-9999999'], generate_segments(plan(hi=9999999)['segments'], None))
        self.assertEqual(['pid:0-4', 'pid:5-9'], _int_children('pid:0-9'))
        self.assertEqual(['pid:0-5', 'pid:6-10'], _int_children('pid:0-10'))
        self.assertEqual(['pid:7-7', 'pid:8-8'], _int_children('pid:7-8'))
        self.assertEqual([], _int_children('pid:7-7'))
        spans = [(0, 9999999)]
        for _ in range(6):
            spans = [s for lo, hi in spans for s in
                     [tuple(map(int, c.split(':')[1].split('-'))) for c in _int_children('pid:%d-%d' % (lo, hi))]]
        self.assertEqual(0, spans[0][0])
        self.assertEqual(9999999, spans[-1][1])
        self.assertTrue(all(b[0] == a[1] + 1 for a, b in zip(spans, spans[1:])))

    def test_bisection_makes_exactly_the_reference_leaves_and_sums_to_total(self):
        pids = list(range(0, 300, 3)) + list(range(400, 420)) + [1000, 1001, 1002]
        total = lambda lo, hi: sum(lo <= p <= hi for p in pids)
        api = API(pids)
        result = self.env.run(plan(), None, transport=FakeTransport(api))
        self.assertEqual('complete', result['status'], result)
        expected = {'pid:%d-%d' % leaf for leaf in reference_leaves(total)}
        leaves = self.leaves()
        self.assertEqual(expected, set(leaves))
        self.assertTrue(all(s['status'] == 'complete' and s['evidence'] == 'total_matched' for s in leaves.values()))
        self.assertEqual(len(pids), sum(s['reported_total'] for s in leaves.values()))
        self.assertEqual(len(pids), len(self.env.docs()))
        self.assertEqual(len(pids), self.env.store.site(self.env.site_id)['state']['overall_total'])
        self.assertTrue(all(r.startswith('pid:[') for r in api.ranges() if r))

    def test_empty_side_completes_by_total_zero_without_walking_pages(self):
        pids = list(range(0, 40))   # everything in the low half; [512, 1023] is empty
        api = API(pids)
        result = self.env.run(plan(), None, transport=FakeTransport(api))
        self.assertEqual('complete', result['status'], result)
        empty = [s for s in self.leaves().values() if s['reported_total'] == 0]
        self.assertTrue(empty)
        self.assertTrue(all(s['status'] == 'complete' and s['evidence'] == 'total_matched' and s['listed'] == 0
                            for s in empty))
        # An empty range costs the first page plus the standing one-time re-read; page 2 is never asked.
        for key in (k for k, s in self.leaves().items() if s['reported_total'] == 0):
            lo, hi = key.split(':')[1].split('-')
            asked = [u for u in api.urls if parse_qs(urlsplit(u).query).get('q') == ['pid:[%s TO %s]' % (lo, hi)]]
            self.assertEqual(2, len(asked), key)
            self.assertFalse([u for u in asked if 'page=2' in u])
        self.assertEqual(40, len(self.env.docs()))

    def test_one_id_over_cap_is_cap_unsplittable_and_not_complete(self):
        pids = list(range(0, 20)) + [500] * 8      # SONAR pids are unique; this exercises the guard
        api = API(pids)
        result = self.env.run(plan(cap=5), None, transport=FakeTransport(api))
        self.assertNotEqual('complete', result['status'], result)
        self.assertEqual('complete_with_mismatch', result['status'])
        stuck = self.leaves()['pid:500-500']
        self.assertEqual('cap_unsplittable', stuck['status'])
        self.assertEqual(8, stuck['reported_total'])
        self.assertIn('pid:500-500', result['stop_reason'])
        # It never walked into the capped window.
        self.assertEqual(0, stuck['listed'])

    def test_resume_reproduces_the_same_leaf_set(self):
        pids = list(range(0, 300, 3)) + list(range(400, 420))
        whole = Env()
        self.addCleanup(whole.close)
        self.assertEqual('complete', whole.run(plan(), None, transport=FakeTransport(API(pids)))['status'])
        site = whole.store.site(whole.site_id)
        reference = {s['segment_key'] for s in whole.store.segments(whole.site_id, site['plan_fp'])}
        api = API(pids)
        first = self.env.run(plan(), None, transport=FakeTransport(api), max_requests=6)
        self.assertEqual(('incomplete', 'request_budget'), (first['status'], first['stop_reason']))
        partial = set(self.segments())
        self.assertLess(partial, reference | partial)
        second = self.env.run(plan(), None, transport=FakeTransport(api))
        self.assertEqual('complete', second['status'], second)
        self.assertEqual(reference, set(self.segments()))
        self.assertEqual(len(pids), len(self.env.docs()))

    def test_missing_or_duplicated_hits_are_reported_as_mismatch(self):
        pids = list(range(0, 24))
        # pid 13 is counted in every total but never served: the range shrinks down to that one id.
        api = API(pids, hide=[13])
        result = self.env.run(plan(cap=30), None, transport=FakeTransport(api))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        bad = self.leaves()['pid:13-13']
        self.assertEqual(('mismatch', 1, 0, 'listed_0_vs_total_1'),
                         (bad['status'], bad['reported_total'], bad['listed'], bad['evidence']))
        self.assertIn('pid:13-13', result['stop_reason'])
        self.assertEqual(23, len(self.env.docs()))
        self.assertIn('overall_total', result['stop_reason'])

    def test_overall_total_and_leaf_sum_catch_ranges_outside_the_plan_bounds(self):
        pids = list(range(0, 20)) + [5000]          # 5000 lies beyond hi=1023
        result = self.env.run(plan(), None, transport=FakeTransport(API(pids)))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])

    def test_overall_total_mismatch_is_detected(self):
        result = self.env.run(plan(), None, transport=FakeTransport(API(list(range(10)), overall_delta=3)))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])

    def test_validation(self):
        good = {'sites': {'s': plan()}}
        site_plan(good, 's', min_interval=0)
        for key, value in [('lo', -1), ('lo', True), ('hi', 1.5), ('hi', '10'), ('hidden_cap', 0), ('hidden_cap', None)]:
            bad = copy.deepcopy(good)
            bad['sites']['s']['segments'][key] = value
            with self.assertRaises(PlanError, msg=(key, value)):
                site_plan(bad, 's', min_interval=0)
        bad = copy.deepcopy(good)
        bad['sites']['s']['segments'].update(lo=5, hi=4)
        with self.assertRaises(PlanError):
            site_plan(bad, 's', min_interval=0)
        for key, value in [('param', 'page'), ('param', 'q&x'), ('field', 'pid OR *'), ('to_inclusive', False)]:
            bad = copy.deepcopy(good)
            bad['sites']['s']['segments']['filter'][key] = value
            with self.assertRaises(PlanError, msg=(key, value)):
                site_plan(bad, 's', min_interval=0)
        bad = copy.deepcopy(good)
        bad['sites']['s']['overall_total_check'] = False
        with self.assertRaises(PlanError):
            site_plan(bad, 's', min_interval=0)
        bad = copy.deepcopy(good)
        del bad['sites']['s']['listing']['total']
        with self.assertRaises(PlanError):
            site_plan(bad, 's', min_interval=0)
        bad = copy.deepcopy(good)
        bad['sites']['s']['listing']['params']['q'] = 'scope'
        with self.assertRaises(PlanError):
            site_plan(bad, 's', min_interval=0)
        bad = copy.deepcopy(good)
        bad['sites']['s']['segments'].update(mode='explicit', items=[{'key': 'a'}])
        with self.assertRaises(PlanError):
            site_plan(bad, 's', min_interval=0)

    def test_range_bounds_are_part_of_the_plan_fingerprint(self):
        a = site_plan({'sites': {'s': plan()}}, 's', min_interval=0).fingerprint
        b = site_plan({'sites': {'s': plan(hi=2047)}}, 's', min_interval=0).fingerprint
        self.assertNotEqual(a, b)


if __name__ == '__main__':
    unittest.main()
