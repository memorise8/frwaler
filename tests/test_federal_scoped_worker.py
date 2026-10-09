"""Real PostgreSQL queue lifecycle and native scoped saves with source fixtures."""
import json
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

from delivery.worker import jobs, scopes, worker
from tests.test_federal_date_scheduler import PlanContract
from tests.test_public_api_resume import ApiResumeContract


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')
class FederalScopedWorker(unittest.TestCase):
    SITE = 'federalregister-gov-documents'; BACKEND = 'postgres'
    count = ApiResumeContract.count

    def setUp(self):
        PlanContract.setUp(self)
        from delivery.db.schema import init_delivery_schema
        from crawler.sites import CRAWLERS
        init_delivery_schema(self.conn)
        self.enterContext(patch.dict(CRAWLERS, {self.SITE: self.cls}))
        self.fail_page = False
        def request(crawler, url, **kwargs):
            q = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
            lo, hi = q['conditions[publication_date][gte]'], q['conditions[publication_date][lte]']
            page = int(q['page']); self.calls.append((lo, page))
            if self.fail_page and page == 2:
                return None
            records = [d for d in self.docs if lo <= d['publication_date'] <= hi]
            response = Mock(); response.json.return_value = {'count': len(records),
                'results': records[(page-1)*3:page*3],
                'next_page_url': crawler._API_BASE+'?'+urlencode(dict(q, page=str(page+1))) if page*3 < len(records) else None}
            return response
        self.enterContext(patch.object(self.cls, '_request', request))

    def enqueue(self, *, observation='fixture-observation', limit=3, files=False):
        jid = scopes.enqueue_date_plan(self.conn, site_id=self.SITE, lower='2020-01-01', upper='2020-01-31',
            ranges=self.ranges, observation_id=observation, limit_n=limit, process_files=files)
        self.scope_id = self.conn.execute('SELECT scope_id FROM crawl_jobs WHERE id=%s', (jid,)).fetchone()['scope_id']
        return jid

    def scope(self):
        return scopes.load(self.conn, self.scope_id, self.SITE)

    def run_next(self, **kwargs):
        job = jobs.claim_next_job(self.conn)
        self.assertIsNotNone(job)
        return worker.run_job(self.conn, job, delay=0, **kwargs)

    def assert_no_site_completion(self):
        row = self.conn.execute('SELECT completed_at FROM crawl_site_progress WHERE site_id=%s', (self.SITE,)).fetchone()
        self.assertTrue(row is None or row['completed_at'] is None)

    def test_native_chain_reuses_scope_and_preserves_unrelated_site_cursor(self):
        jobs.save_progress(self.conn, self.SITE, {'legacy': 'unrelated'})
        self.enqueue()
        for expected in (3, 3, 3):
            self.assertEqual(self.run_next(), expected)
            self.assertEqual(jobs.load_cursor(self.conn, self.SITE), {'legacy': 'unrelated'})
        self.assertEqual(self.count(), 9)
        self.assertIsNotNone(self.scope()['completed_at'])
        self.assertEqual(self.scope()['items_done'], 9)
        self.assertIsNone(jobs.claim_next_job(self.conn))
        self.assert_no_site_completion()
        rows = jobs.list_jobs(self.conn)
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['scope_id'] == self.scope_id and r['status'] == 'done' for r in rows))
        detail = jobs.job_detail(self.conn, rows[0]['id'])['scope']
        self.assertEqual((detail['lower'], detail['upper']), ('2020-01-01', '2020-01-31'))
        self.assertIsNotNone(detail['completed_at'])
        self.assertFalse(detail['whole_site_completion'])
        self.assertFalse(detail['files_verified'])

    def test_cancellation_keeps_scope_cursor_and_retry_continues_same_observation(self):
        jid = self.enqueue(limit=None)
        def cancel():
            if self.count() >= 2:
                jobs.cancel_job(self.conn, jid)
                return True
            return False
        self.assertEqual(self.run_next(should_cancel=cancel), 2)
        self.assertEqual(jobs.job_detail(self.conn, jid)['job']['status'], 'cancelled')
        self.assertIsNone(self.scope()['completed_at'])
        self.assertIsNotNone(self.scope()['cursor'])
        self.assertIsNone(jobs.claim_next_job(self.conn))
        retried = jobs.retry_job(self.conn, jid)
        self.assertEqual(retried['scope_id'], self.scope_id)
        self.assertEqual(self.run_next(), 7)
        self.assert_no_site_completion()

    def test_source_failure_stops_chain_and_retry_preserves_committed_documents(self):
        jid = self.enqueue(limit=None); self.fail_page = True
        self.assertEqual(self.run_next(), 3)
        self.assertEqual(jobs.job_detail(self.conn, jid)['job']['status'], 'failed')
        self.assertIsNone(jobs.claim_next_job(self.conn))
        self.assertIsNone(self.scope()['completed_at'])
        self.fail_page = False
        jobs.retry_job(self.conn, jid)
        self.assertEqual(self.run_next(), 6)
        self.assertEqual(self.scope()['items_done'], 9)
        self.assert_no_site_completion()

    def test_budget_chunks_include_zero_save_excluded_range(self):
        for doc in self.docs[:5]:
            doc['abstract'] = 'short'
        original = self.cls.crawl_date_plan
        def bounded(crawler, *args, **kwargs):
            kwargs['max_requests'] = 2
            return original(crawler, *args, **kwargs)
        self.enterContext(patch.object(self.cls, 'crawl_date_plan', bounded))
        self.enqueue(limit=None)
        self.assertEqual(self.run_next(), 0)
        self.assertEqual(self.scope()['cursor']['range_index'], 1)
        self.assertEqual(self.run_next(), 4)
        self.assertIsNotNone(self.scope()['completed_at'])
        self.assert_no_site_completion()

    def test_distinct_observations_have_independent_cursors(self):
        self.enqueue(limit=None); self.run_next(); first = self.scope_id
        self.enqueue(observation='second-observation', limit=None)
        self.assertNotEqual(self.scope_id, first)
        self.assertIsNone(self.scope()['cursor'])
        before = len(self.calls)
        self.assertEqual(self.run_next(), 0)
        self.assertGreater(len(self.calls), before)
        self.assertIsNotNone(self.scope()['completed_at'])
        self.assert_no_site_completion()

    def test_completed_observation_reuse_makes_no_new_requests(self):
        self.enqueue(limit=None); self.run_next(); before = len(self.calls)
        self.enqueue(limit=None)
        self.assertEqual(self.run_next(), 0)
        self.assertEqual(len(self.calls), before)
        self.assert_no_site_completion()

    def test_modified_definition_or_source_version_fails_before_fetch(self):
        jid = self.enqueue()
        self.conn.execute("UPDATE crawl_scopes SET definition=jsonb_set(definition,'{observation_id}','\"changed\"') WHERE scope_id=%s", (self.scope_id,)); self.conn.commit()
        self.assertEqual(self.run_next(), 0)
        self.assertEqual(jobs.job_detail(self.conn, jid)['job']['status'], 'failed')
        self.assertEqual(self.calls, [])

    def test_changed_source_hash_is_not_silently_restarted(self):
        jid = self.enqueue()
        with patch.object(scopes, 'source_hash_for_class', return_value='changed'):
            self.assertEqual(self.run_next(), 0)
        self.assertEqual(jobs.job_detail(self.conn, jid)['job']['status'], 'failed')
        self.assertEqual(self.calls, [])
        self.assertIsNone(self.scope()['cursor'])

    def test_invalid_scope_is_rejected_before_queueing(self):
        self.ranges[1]['lower'] = '2020-01-17'
        with self.assertRaises(ValueError): self.enqueue()
        self.assertEqual(jobs.list_jobs(self.conn), [])
        self.assertEqual(self.calls, [])

    def test_whole_site_backfill_remains_disallowed(self):
        from delivery.capabilities import capabilities
        self.assertFalse(capabilities(self.SITE)['modes']['backfill']['allowed'])
        jid = jobs.enqueue_job(self.conn, self.SITE, mode='backfill')
        self.assertEqual(self.run_next(), 0)
        self.assertEqual(jobs.job_detail(self.conn, jid)['job']['status'], 'failed')
        self.assertEqual(self.calls, [])

    def test_file_queue_flag_is_preserved_across_automatic_chunks(self):
        self.enqueue(files=True)
        for _ in range(3): self.run_next()
        self.assertTrue(all(j['process_files'] for j in jobs.list_jobs(self.conn)))
        count = self.conn.execute('SELECT count(DISTINCT seq_id) AS n FROM document_file_jobs').fetchone()['n']
        self.assertEqual(count, 9)
        self.assert_no_site_completion()

    def test_unexpected_exception_preserves_saved_count_and_scope_checkpoint(self):
        jid = self.enqueue()
        original = self.cls.crawl_date_plan
        def crash(crawler, *args, **kwargs):
            original(crawler, *args, **kwargs)
            raise RuntimeError('controlled post-chunk failure')
        with patch.object(self.cls, 'crawl_date_plan', crash):
            self.assertEqual(self.run_next(), 3)
        row = jobs.job_detail(self.conn, jid)['job']
        self.assertEqual((row['status'], row['saved_count']), ('failed', 3))
        self.assertIsNotNone(self.scope()['cursor'])
        self.assertIsNone(jobs.claim_next_job(self.conn))
        jobs.retry_job(self.conn, jid)
        self.assertEqual(self.run_next(), 3)
        self.assertEqual(self.run_next(), 3)
        self.assert_no_site_completion()

    def test_cancel_on_normal_return_does_not_mark_scope_complete(self):
        jid = self.enqueue(limit=None); original = self.cls.crawl_date_plan
        def cancel(crawler, *args, **kwargs):
            result = original(crawler, *args, **kwargs)
            jobs.cancel_job(self.conn, jid)
            return result
        with patch.object(self.cls, 'crawl_date_plan', cancel):
            self.assertEqual(self.run_next(), 9)
        self.assertEqual(jobs.job_detail(self.conn, jid)['job']['status'], 'cancelled')
        self.assertIsNone(self.scope()['completed_at'])
        self.assertIsNotNone(self.scope()['cursor'])
        self.assertIsNone(jobs.claim_next_job(self.conn))
        jobs.retry_job(self.conn, jid)
        self.assertEqual(self.run_next(), 0)
        self.assertIsNotNone(self.scope()['completed_at'])
        self.assert_no_site_completion()

    def test_lost_terminal_transition_does_not_persist_cursor_or_requeue(self):
        self.enqueue()
        with patch.object(jobs, 'finish_job', return_value=None):
            self.assertEqual(self.run_next(), 3)
        self.assertIsNone(self.scope()['cursor'])
        self.assertIsNone(self.scope()['completed_at'])
        self.assertEqual(len(jobs.list_jobs(self.conn)), 1)
        self.assert_no_site_completion()

    def test_empty_ranges_advance_without_saved_or_observed_items(self):
        self.docs = []
        self.ranges = [{'lower':'2020-01-01','upper':'2020-01-10','expected_count':0},
                       {'lower':'2020-01-11','upper':'2020-01-20','expected_count':0},
                       {'lower':'2020-01-21','upper':'2020-01-31','expected_count':0}]
        original = self.cls.crawl_date_plan
        def bounded(crawler, *args, **kwargs):
            kwargs['max_requests'] = 2
            return original(crawler, *args, **kwargs)
        with patch.object(self.cls, 'crawl_date_plan', bounded):
            self.enqueue(limit=None)
            self.assertEqual(self.run_next(), 0)
            self.assertEqual(self.scope()['cursor']['range_index'], 2)
            self.assertEqual(self.scope()['items_done'], 0)
            self.assertEqual(self.run_next(), 0)
        self.assertIsNotNone(self.scope()['completed_at'])
        self.assert_no_site_completion()

    def test_existing_whole_site_completion_is_never_changed(self):
        jobs.save_progress(self.conn, self.SITE, {'legacy': 'complete'})
        jobs.mark_backfill_complete(self.conn, self.SITE)
        before = self.conn.execute('SELECT * FROM crawl_site_progress WHERE site_id=%s', (self.SITE,)).fetchone()
        self.enqueue(limit=None); self.run_next()
        after = self.conn.execute('SELECT * FROM crawl_site_progress WHERE site_id=%s', (self.SITE,)).fetchone()
        self.assertEqual(before, after)

    def test_scoped_retry_cannot_overlap_another_active_observation(self):
        jid = self.enqueue(); jobs.cancel_job(self.conn, jid)
        second = self.enqueue(observation='another-observation')
        with self.assertRaises(jobs.ActiveJobError):
            jobs.retry_job(self.conn, jid)
        self.assertIsNone(jobs.job_detail(self.conn, jid)['job']['retried_by'])
        self.assertEqual(jobs.job_detail(self.conn, second)['job']['status'], 'queued')
