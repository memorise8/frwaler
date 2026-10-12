"""Shared incremental invariants, using a real durable isolated DB."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from crawler.base_crawler import BaseCrawler, CrawlCancelled
from crawler.incremental import Collector, Entry, Page, Policy, Store, init_schema, observe_order, infer_order


class Crawler(BaseCrawler):
    site_id = 'test-source'
    site_name = 'test'
    base_url = 'https://example.invalid'

    def crawl(self, limit=None):
        pass

    def _save_paper(self, paper):
        self._check_cancelled()
        if getattr(self, 'fail_key', None) == paper['external_id']:
            raise RuntimeError('injected save failure')
        self._last_save_created = self._conn.execute('SELECT 1 FROM docs WHERE id=?', (paper['external_id'],)).fetchone() is None
        self._conn.execute('INSERT INTO docs VALUES(?,?) ON CONFLICT(id) DO NOTHING', (paper['external_id'],json.dumps(paper)))
        self._conn.commit()
        self.saved_order.append(paper['external_id'])


class Source:
    contract = 'fixture-v1'
    numeric_ids = True
    stable_ids = True
    can_read_newest = True
    detail_requests = 0

    def __init__(self, crawler, ids, ascending=False, pins=()):
        self.crawler, self.ids, self.ascending, self.pins = crawler, ids, ascending, pins
        self.calls = []

    def fetch(self, cursor, direction):
        pos = (cursor or {}).get('position', 0)
        ids = sorted(self.ids, reverse=not self.ascending if direction == 'native' else True)
        if self.pins:
            ids = list(self.pins) + [v for v in ids if v not in self.pins]
        batch = ids[pos:pos+3]
        self.calls.append((pos, direction))
        return Page([Entry(str(i), {'external_id':str(i), 'title':str(i)}, pinned=i in self.pins) for i in batch],
                    {'position':pos+len(batch)}, pos+len(batch) == len(ids), 'fixture end')

    def exists(self, entry):
        return self.crawler._conn.execute('SELECT 1 FROM docs WHERE id=?',(entry.key,)).fetchone() is not None

    def materialize(self, payload):
        self.detail_requests += 1
        return payload


class IncrementalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'isolated.db'
        self.conn = sqlite3.connect(self.path)
        init_schema(self.conn)
        self.conn.execute('CREATE TABLE docs(id TEXT PRIMARY KEY,payload TEXT)')
        self.c = self.crawler()
        self.policy = Policy(max_pages=100, full_check_days=0)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def crawler(self):
        c = Crawler(self.conn, delay=0)
        c.saved_order = []
        return c

    def run_source(self, ids, ascending=False, pins=(), limit=None, policy=None):
        source = Source(self.c, ids, ascending, pins)
        count = Collector(self.c,source,policy or self.policy).run(limit)
        return count, source

    def state(self):
        return Store(self.conn,self.c.site_id).state()

    def test_newest_first_full_then_zero_details_and_two_pages(self):
        count,_ = self.run_source(list(range(1,31)))
        self.assertEqual(count,30)
        self.assertEqual(self.c.saved_order,[str(i) for i in range(1,31)])
        self.assertTrue(self.state()['baseline_complete'])
        self.assertEqual(self.state()['baseline']['id'],'30')
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,31)))
        self.assertEqual(count,0)
        self.assertEqual(len(source.calls),2)
        self.assertEqual(source.detail_requests,0)
        self.assertFalse(self.c.delivery_exhausted)
        self.assertEqual(self.state()['last_run']['stop_reason'],'known_boundary_plus_overlap')

    def test_oldest_first_automatically_reads_newest_end_on_repeat(self):
        self.run_source(list(range(1,31)),ascending=True)
        self.assertEqual(self.state()['order'],'oldest_first')
        self.assertEqual(self.state()['baseline']['id'],'30')
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,34)),ascending=True)
        self.assertEqual(count,3)
        self.assertEqual(self.c.saved_order,['31','32','33'])
        self.assertEqual(len(source.calls),3)
        self.assertTrue(all(d == 'newest' for _,d in source.calls))

    def test_adapter_can_veto_merged_boundary_until_other_streams_are_safe(self):
        self.run_source(list(range(1,31)))
        self.c = self.crawler()
        source = Source(self.c, list(range(1,31)))
        native = source.fetch
        def guarded(cursor, direction):
            page = native(cursor, direction)
            page.boundary_safe = page.next_cursor['position'] >= 9
            return page
        source.fetch = guarded
        self.assertEqual(Collector(self.c,source,self.policy).run(),0)
        self.assertEqual(len(source.calls),3)
        self.assertEqual(source.detail_requests,0)
        self.assertEqual(self.state()['last_run']['stop_reason'],'known_boundary_plus_overlap')

    def test_known_pins_do_not_hide_new_records(self):
        self.run_source(list(range(1,31)),pins=(1,2,3))
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,34)),pins=(1,2,3))
        self.assertEqual(count,3)
        self.assertEqual(self.c.saved_order,['31','32','33'])
        self.assertEqual(len(source.calls),4)

    def test_new_item_in_overlap_extends_boundary(self):
        self.run_source(list(range(1,31)))
        self.conn.execute("DELETE FROM docs WHERE id='25'");self.conn.commit()
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,31)))
        self.assertEqual(count,1)
        self.assertEqual(len(source.calls),4)
        self.assertEqual(self.c.saved_order,['25'])

    def test_pins_only_page_does_not_consume_overlap_after_restart(self):
        self.run_source(list(range(1,31)))
        # The first ordinary page is all known. A page containing only pins
        # cannot prove that we have inspected the neighbouring ordinary page.
        self.conn.execute("DELETE FROM docs WHERE id='27'");self.conn.commit()
        batches = [(30,29,28), (1,2,3), (27,26,25), (24,23,22), (21,20,19)]
        def source():
            adapter = Source(self.c, [])
            def fetch(cursor, direction):
                index = (cursor or {}).get('index', 0)
                adapter.calls.append(index)
                return Page([Entry(str(i), {'external_id':str(i),'title':str(i)},
                                   pinned=index == 1) for i in batches[index]],
                            {'index':index+1})
            adapter.fetch = fetch
            return adapter
        self.c = self.crawler()
        first = source()
        Collector(self.c,first,Policy(max_pages=2,full_check_days=0)).run()
        self.assertEqual(self.state()['phase'],'discover')
        self.assertEqual(self.state()['overlap'],1)
        self.conn.close();self.conn = sqlite3.connect(self.path)
        self.c = self.crawler()
        resumed = source()
        self.assertEqual(Collector(self.c,resumed,self.policy).run(),1)
        self.assertEqual(resumed.calls,[2,3,4])
        self.assertEqual(self.c.saved_order,['27'])
        self.assertEqual(self.state()['last_run']['stop_reason'],'known_boundary_plus_overlap')

    def test_limit_is_not_completion_and_restart_drains_without_requests(self):
        self.run_source(list(range(1,10)),limit=2)
        self.assertFalse(self.state().get('baseline_complete',False))
        self.assertEqual(self.c.saved_order,['1','2'])
        self.conn.close();self.conn = sqlite3.connect(self.path)
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,10)))
        self.assertEqual(count,7)
        self.assertEqual(source.calls,[])
        self.assertEqual(self.c.saved_order,[str(i) for i in range(3,10)])
        self.assertTrue(self.state()['baseline_complete'])

    def test_discovery_chunk_resumes_before_completion(self):
        self.run_source(list(range(1,10)),policy=Policy(max_pages=1,full_check_days=0))
        self.assertEqual(self.state()['phase'],'discover')
        self.assertFalse(self.state().get('baseline_complete',False))
        self.assertEqual(self.c.saved_order,[])
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,10)))
        self.assertEqual(count,9)
        self.assertEqual(source.calls[0][0],3)

    def test_failed_save_retries_oldest_unsaved(self):
        self.c.fail_key='3'
        with self.assertRaises(RuntimeError):self.run_source(list(range(1,10)))
        self.assertEqual(self.c.saved_order,['1','2'])
        self.assertFalse(self.state().get('baseline_complete',False))
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,10)))
        self.assertEqual(count,7)
        self.assertEqual(source.calls,[])
        self.assertEqual(self.c.saved_order[0],'3')

    def test_deleted_recent_three_recovered_without_all_details(self):
        self.run_source(list(range(1,31)))
        self.conn.execute('DELETE FROM docs WHERE CAST(id AS INTEGER)>27');self.conn.commit()
        self.c = self.crawler()
        count,source = self.run_source(list(range(1,31)))
        self.assertEqual(count,3)
        self.assertEqual(source.detail_requests,3)
        self.assertEqual(len(source.calls),3)

    def test_ambiguous_order_and_unstable_identity_force_full_check(self):
        for stable,numeric in [(False,True),(True,False)]:
            with self.subTest(stable=stable):
                self.conn.execute('DELETE FROM crawl_incremental_state');self.conn.commit()
                source=Source(self.c,list(range(1,10)))
                source.stable_ids=stable;source.numeric_ids=numeric
                Collector(self.c,source,self.policy).run()
                self.assertEqual(self.state()['support'],'full_rescan_required')
                source.calls=[]
                Collector(self.c,source,self.policy).run()
                self.assertEqual(len(source.calls),3)
                self.assertEqual(source.calls[0][1],'native')

    def test_periodic_full_check_records_changes_and_missing_only_at_end(self):
        self.run_source(list(range(1,31)))
        self.c=self.crawler()
        source=Source(self.c,list(range(1,30)))
        original=source.materialize
        source.materialize=lambda d:dict(original(d),title='edited')
        Collector(self.c,source,Policy(full_check_days=1e-15)).run(limit=1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM crawl_incremental_changes WHERE kind='missing'").fetchone()[0],1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM crawl_incremental_changes WHERE kind='modified'").fetchone()[0],29)
        self.assertEqual(self.conn.execute('SELECT count(*) FROM docs').fetchone()[0],30) # audit, no hard delete

    def test_failed_full_check_does_not_mark_source_deletions(self):
        self.run_source(list(range(1,10)))
        self.c=self.crawler();self.c.delivery_mode='full';self.c.fail_key='3'
        with self.assertRaises(RuntimeError):self.run_source(list(range(1,9)))
        self.assertEqual(self.conn.execute("SELECT count(*) FROM crawl_incremental_changes WHERE kind='missing'").fetchone()[0],0)
        self.c=self.crawler();self.c.delivery_mode='full'
        self.run_source(list(range(1,9)))
        self.assertEqual(self.conn.execute("SELECT item_key FROM crawl_incremental_changes WHERE kind='missing'").fetchone()[0],'9')

    def test_invalid_end_and_cancel_never_mark_complete(self):
        source=Source(self.c,[])
        source.fetch=lambda *args:Page([],None,True,'')
        with self.assertRaises(ValueError):Collector(self.c,source,self.policy).run()
        self.assertFalse(self.state().get('baseline_complete',False))
        self.c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):Collector(self.c,Source(self.c,[1]),self.policy).run()
        self.assertFalse(self.state().get('baseline_complete',False))

    def test_changed_incremental_order_is_downgraded_before_known_stop(self):
        self.run_source(list(range(1,10)))
        source=Source(self.c,list(range(1,10)))
        source.fetch=lambda *args:Page([Entry(str(i),{'external_id':str(i)}) for i in [1,2,3]],{'position':3})
        with self.assertRaisesRegex(ValueError,'order_changed'):Collector(self.c,source,self.policy).run()
        self.assertEqual(self.state()['support'],'full_rescan_required')
        self.assertEqual(self.state()['unsupported_reason'],'newest_listing_order_changed')

    def test_contract_change_never_reuses_old_baseline(self):
        self.run_source(list(range(1,10)))
        source=Source(self.c,list(range(1,10)));source.contract='different-scope'
        with self.assertRaisesRegex(ValueError,'contract changed'):Collector(self.c,source,self.policy).run()

    def test_exact_dates_infer_both_directions_without_numeric_ids(self):
        for dates, expected in [(['2026-01-01', '2026-01-02'], 'oldest_first'),
                                (['2026-01-02', '2026-01-01'], 'newest_first')]:
            with self.subTest(expected=expected):
                proof = {}
                # A pin's old date must not change the ordinary listing proof.
                observe_order(proof, [Entry('pin', {}, '2000-01-01', True),
                                      Entry('alpha', {}, dates[0])], False)
                observe_order(proof, [Entry('beta', {}, dates[1])], False)
                self.assertEqual(infer_order(proof), (expected, 'date'))

    def test_first_last_dates_alone_cannot_certify_mixed_or_missing_order(self):
        for dates in [['2026-01-03', '2026-01-01', '2026-01-02'],
                      ['2026-01-03', '', '2026-01-01'], ['2026', '2025']]:
            with self.subTest(dates=dates):
                proof = {}
                observe_order(proof, [Entry(str(i), {}, d) for i,d in enumerate(dates)], False)
                self.assertEqual(infer_order(proof), ('arbitrary', 'order_ambiguous'))


if __name__=='__main__':unittest.main()
