"""String-prefix half-open range tree (query_prefix_range) against a lexicographic fake server. No network."""
import copy
import json
import random
import unittest
from bisect import bisect_left
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.plan import PlanError, site_plan
from crawler.enumeration.strategies import _prefix_children, generate_segments
from tests.enumeration_fakes import Env, FakeTransport

PAGE = 10


def plan(cap=25, max_depth=9, **over):
    site = {
        'strategy': 'date_range_split', 'request_interval_s': 0, 'adapter': {'type': 'list_only'},
        'listing': {'url': 'https://pid.test/api', 'params': {'scope': 'x', 'size': PAGE}, 'page_param': 'page',
                    'start_page': 1,
                    'parser': {'type': 'json', 'items_path': 'hits.hits', 'key': 'id',
                               'url_template': 'https://pid.test/doc/{id}', 'title': 'title'},
                    'total': {'type': 'json', 'path': 'hits.total.value'}},
        'segments': {'mode': 'range', 'hidden_cap': cap, 'max_depth': max_depth,
                     'filter': {'type': 'query_prefix_range', 'param': 'q', 'field': 'pid'}},
        'overall_total_check': True}
    site.update(over)
    return site


def parse_query(q):
    """'pid:[A TO B}' -> (A, B); anything else raises."""
    assert q.startswith('pid:[') and q.endswith('}') and ' TO ' in q, q
    return tuple(q[len('pid:['):-1].split(' TO '))


class API:
    """The observed server: pid is compared as a string (lexicographic); [A TO B} is half-open."""

    def __init__(self, pids, *, hide=(), overall_delta=0):
        self.docs = [{'id': 'd%d' % i, 'pid': p, 'title': 't'} for i, p in enumerate(pids)]
        self.hide = set(hide)
        self.overall_delta = overall_delta
        self.urls = []

    def queries(self):
        return [parse_qs(urlsplit(u).query).get('q', [''])[0] for u in self.urls if '/api' in u]

    def __call__(self, url):
        self.urls.append(url)
        u = urlsplit(url)
        if u.path == '/robots.txt':
            return Response(404, {}, b'')
        q = {k: v[0] for k, v in parse_qs(u.query, keep_blank_values=True).items()}
        rows = self.docs
        if q.get('q'):
            lo, hi = parse_query(q['q'])
            rows = [d for d in rows if lo <= d['pid'] < hi]
        total = len(rows) + (0 if q.get('q') else self.overall_delta)
        rows = sorted(rows, key=lambda d: (d['pid'], d['id']))
        served = [d for d in rows if d['pid'] not in self.hide]
        page = int(q['page'])
        body = {'hits': {'total': {'value': total, 'relation': 'eq'}, 'hits': served[(page - 1) * PAGE: page * PAGE]}}
        return Response(200, {'Content-Type': 'application/json'}, json.dumps(body), url)


def synthetic_pids(seed=7, n=900):
    """Mixed 5-7 digit ids, 55% starting with 1, all distinct, no leading zero except via the digit 0 bucket."""
    rnd = random.Random(seed)
    out = set()
    while len(out) < n:
        length = rnd.choice((5, 6, 7))
        first = '1' if rnd.random() < 0.55 else rnd.choice('023456789')
        out.add(first + ''.join(rnd.choice('0123456789') for _ in range(length - 1)))
    return sorted(out)


def model_leaves(pids, cap, max_depth):
    """Independent model of the rule, written from the specification with sorted-list counting."""
    def total(lo, hi):
        return bisect_left(pids, hi) - bisect_left(pids, lo)

    def kids(lo, hi):
        if (lo, hi) == ('0', ':'):
            return [(str(d), str(d + 1) if d < 9 else ':') for d in range(10)]
        if len(lo) >= max_depth or hi == lo + '0':
            return []
        return [(lo, lo + '0')] + [(lo + str(d), lo + str(d + 1)) for d in range(9)] + [(lo + '9', hi)]

    def rec(lo, hi):
        if total(lo, hi) <= cap or not kids(lo, hi):
            return [(lo, hi)]
        return [leaf for a, b in kids(lo, hi) for leaf in rec(a, b)]
    return rec('0', ':')


class PrefixRange(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.close()

    def segments(self):
        site = self.env.store.site(self.env.site_id)
        return {s['segment_key']: s for s in self.env.store.segments(self.env.site_id, site['plan_fp'])}

    def leaves(self):
        return {k: s for k, s in self.segments().items() if s['status'] != 'split'}

    def test_root_children_and_query_syntax(self):
        self.assertEqual(['pid:0-:'], generate_segments(plan()['segments'], None))
        self.assertEqual(['pid:%d-%s' % (d, str(d + 1) if d < 9 else ':') for d in range(10)],
                         _prefix_children('pid:0-:', 9))
        kids = _prefix_children('pid:12-13', 9)
        self.assertEqual(['pid:12-120'] + ['pid:12%d-12%d' % (d, d + 1) for d in range(9)] + ['pid:129-13'], kids)
        self.assertEqual(11, len(kids))
        # The last child keeps the parent's upper bound, also below the last-digit boundary.
        self.assertEqual('pid:199-2', _prefix_children('pid:19-2', 9)[-1])
        self.assertEqual('pid:9999-:', _prefix_children('pid:999-:', 9)[-1])
        self.assertEqual([], _prefix_children('pid:12-120', 9))          # first child: only the id itself
        self.assertEqual([], _prefix_children('pid:123456789-123456790', 9))   # max depth
        self.assertEqual(11, len(_prefix_children('pid:12345678-12345679', 9)))

    def test_every_split_is_disjoint_and_complete_over_string_order(self):
        # For each parent and a pool of strings, children partition exactly the parent's members.
        pool = synthetic_pids() + ['1', '12', '19', '199', '2', '0', '09', '9', '99', '999', '1x', '12/', '12:']
        for parent in ('pid:0-:', 'pid:1-2', 'pid:12-13', 'pid:19-2', 'pid:199-2', 'pid:9-:', 'pid:99-:'):
            _, lo, hi = parent.split(':')[0], *parent.split(':', 1)[1].split('-')
            members = [s for s in pool if lo <= s < hi]
            kids = [tuple(k.split(':', 1)[1].split('-')) for k in _prefix_children(parent, 9)]
            counted = [[s for s in members if a <= s < b] for a, b in kids]
            self.assertEqual(len(members), sum(map(len, counted)), parent)
            self.assertEqual(sorted(members), sorted(s for c in counted for s in c), parent)
            self.assertEqual(lo if parent != 'pid:0-:' else '0', kids[0][0])
            self.assertEqual(hi, kids[-1][1])
            self.assertTrue(all(x[1] == y[0] for x, y in zip(kids, kids[1:])))

    def test_tree_equals_independent_model_and_sums_to_total(self):
        pids = synthetic_pids()
        api = API(pids)
        result = self.env.run(plan(), None, transport=FakeTransport(api))
        self.assertEqual('complete', result['status'], result)
        expected = {'pid:%s-%s' % leaf for leaf in model_leaves(pids, 25, 9)}
        leaves = self.leaves()
        self.assertEqual(expected, set(leaves))
        self.assertTrue(all(s['status'] == 'complete' and s['evidence'] == 'total_matched' for s in leaves.values()))
        self.assertTrue(all(s['reported_total'] <= 25 for s in leaves.values()))
        self.assertEqual(len(pids), sum(s['reported_total'] for s in leaves.values()))
        self.assertEqual(len(pids), len(self.env.docs()))
        self.assertEqual(len(pids), self.env.store.site(self.env.site_id)['state']['overall_total'])
        self.assertEqual('pid:[0 TO :}', api.queries()[0])
        # Dense first-digit buckets forced multi-level splits.
        self.assertTrue(any(len(k.split(':', 1)[1].split('-')[0]) >= 3 for k in leaves))

    def test_first_child_holds_one_id_and_always_fits_the_cap(self):
        pids = ['1', '12', '123'] + ['12%d' % d for d in range(10)] + ['1230', '1231']
        pids = sorted(set(pids))
        api = API(pids)
        result = self.env.run(plan(cap=1), None, transport=FakeTransport(api))
        self.assertEqual('complete', result['status'], result)
        leaves = self.leaves()
        for lo, hi in (('1', '10'), ('12', '120'), ('123', '1230')):
            self.assertEqual(1, leaves['pid:%s-%s' % (lo, hi)]['reported_total'])
            self.assertEqual('complete', leaves['pid:%s-%s' % (lo, hi)]['status'])
        self.assertEqual(len(pids), len(self.env.docs()))

    def test_empty_ranges_complete_by_total_zero_without_page_two(self):
        pids = ['1%04d' % i for i in range(60)]
        api = API(pids)
        result = self.env.run(plan(), None, transport=FakeTransport(api))
        self.assertEqual('complete', result['status'], result)
        empty = [k for k, s in self.leaves().items() if s['reported_total'] == 0]
        self.assertTrue(empty)
        for key in empty:
            lo, hi = key.split(':', 1)[1].split('-')
            asked = [u for u in api.urls if parse_qs(urlsplit(u).query).get('q') == ['pid:[%s TO %s}' % (lo, hi)]]
            self.assertEqual(['1', '1'], [parse_qs(urlsplit(u).query)['page'][0] for u in asked], key)
            self.assertEqual(('complete', 'total_matched', 0),
                             tuple(self.leaves()[key][f] for f in ('status', 'evidence', 'listed')))

    def test_depth_limit_is_cap_unsplittable_not_completion(self):
        pids = ['123%d' % d for d in range(10)] + ['2%03d' % (d * 10) for d in range(10)]
        result = self.env.run(plan(cap=5, max_depth=3), None, transport=FakeTransport(API(pids)))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        stuck = [k for k, s in self.leaves().items() if s['status'] == 'cap_unsplittable']
        self.assertEqual(['pid:123-124'], stuck)
        self.assertEqual(0, self.leaves()['pid:123-124']['listed'])      # did not walk into the window
        self.assertIn('pid:123-124', result['stop_reason'])
        # One more level is enough for the same data.
        other = Env()
        self.addCleanup(other.close)
        self.assertEqual('complete', other.run(plan(cap=5, max_depth=4), None, transport=FakeTransport(API(pids)))['status'])

    def test_resume_reproduces_the_same_range_set(self):
        pids = synthetic_pids(n=300)
        whole = Env()
        self.addCleanup(whole.close)
        self.assertEqual('complete', whole.run(plan(cap=20), None, transport=FakeTransport(API(pids)))['status'])
        site = whole.store.site(whole.site_id)
        reference = {s['segment_key'] for s in whole.store.segments(whole.site_id, site['plan_fp'])}
        api = API(pids)
        first = self.env.run(plan(cap=20), None, transport=FakeTransport(api), max_requests=8)
        self.assertEqual(('incomplete', 'request_budget'), (first['status'], first['stop_reason']))
        self.assertLess(len(self.segments()), len(reference))
        second = self.env.run(plan(cap=20), None, transport=FakeTransport(api))
        self.assertEqual('complete', second['status'], second)
        self.assertEqual(reference, set(self.segments()))
        self.assertEqual(len(pids), len(self.env.docs()))

    def test_non_digit_or_non_digit_leading_ids_surface_as_overall_mismatch(self):
        pids = ['1%04d' % i for i in range(30)] + ['abc', 'X1', '-5', ' 7', '']
        result = self.env.run(plan(), None, transport=FakeTransport(API(pids)))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])
        self.assertEqual(30, sum(s['reported_total'] for s in self.leaves().values()))
        self.assertEqual(35, self.env.store.site(self.env.site_id)['state']['overall_total'])

    def test_unserved_id_and_wrong_overall_total_are_mismatches(self):
        pids = ['1%03d' % i for i in range(40)]
        result = self.env.run(plan(), None, transport=FakeTransport(API(pids, hide=['1017'])))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        bad = self.leaves()['pid:1017-10170']
        self.assertEqual(('mismatch', 1, 0, 'listed_0_vs_total_1'),
                         (bad['status'], bad['reported_total'], bad['listed'], bad['evidence']))
        self.assertEqual(39, len(self.env.docs()))
        other = Env()
        self.addCleanup(other.close)
        result = other.run(plan(), None, transport=FakeTransport(API(pids, overall_delta=3)))
        self.assertEqual('complete_with_mismatch', result['status'], result)
        self.assertIn('overall_total', result['stop_reason'])

    def test_integer_bisection_is_wrong_on_a_string_ordered_server(self):
        """Regression for the rejected design: mid=(lo+hi)//2 closed ranges overlap under string order."""
        pids = synthetic_pids()

        def total(lo, hi):          # server model: closed [lo, hi], bounds compared as strings
            return sum(str(lo) <= p <= str(hi) for p in pids)
        # The precheck's digit-length ranges each reported nearly the whole set: heavy overlap.
        self.assertGreater(total(0, 99999) + total(100000, 999999) + total(1000000, 9999999), 2 * len(pids))
        # One integer bisection step: the two halves do not add up to the parent (ids are lost or doubled).
        mid = (0 + 9999999) // 2
        self.assertNotEqual(total(0, 9999999), total(0, mid) + total(mid + 1, 9999999))
        # A deeper step overlaps outright: 5-digit and 7-digit ids interleave in string order.
        self.assertGreater(total(1000000, 1999999) + total(1000, 1999999), total(1000, 1999999))
        self.assertGreater(total(0, 499999) + total(250000, 999999), total(0, 999999))

    def test_validation_and_fingerprint(self):
        good = {'sites': {'s': plan()}}
        base = site_plan(good, 's', min_interval=0).fingerprint
        for key, value in [('hidden_cap', 0), ('hidden_cap', None), ('max_depth', 0), ('max_depth', 19),
                           ('max_depth', None), ('max_depth', True)]:
            bad = copy.deepcopy(good)
            bad['sites']['s']['segments'][key] = value
            with self.assertRaises(PlanError, msg=(key, value)):
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
        deeper = site_plan({'sites': {'s': plan(max_depth=8)}}, 's', min_interval=0).fingerprint
        self.assertNotEqual(base, deeper)
        old = copy.deepcopy(good)
        old['sites']['s']['segments']['filter']['type'] = 'query_int_range'
        with self.assertRaises(PlanError):
            site_plan(old, 's', min_interval=0)


if __name__ == '__main__':
    unittest.main()
