"""Default worker integration with hash-verified actual source captures."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

DSN = os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE = os.environ.get('INCREMENTAL_ARCHIVES_SOURCE')


@unittest.skipUnless(DSN and SOURCE, 'requires fresh owned PostgreSQL DB and retained Archives HTML')
class ArchivesWorkerTests(unittest.TestCase):
    def test_default_worker_uses_shared_engine_and_native_metadata(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        from delivery.scripts.review_archives_nationales_incremental import retained_responses, native_papers
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn = db_pg.open_db(DSN); self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn); init_delivery_schema(conn)
        cls = CRAWLERS['archives-nationales-culture-gouv-fr-presse']
        db_pg.upsert_site(conn, cls.site_id, cls.site_name, cls.base_url)
        responses, manifest = retained_responses(SOURCE)
        papers = {p['external_id']: p for p in native_papers(responses)}
        calls, phases = [], []
        def request(c, url, **kwargs):
            calls.append(url); return Mock(text=responses[url])
        with patch.dict(os.environ, {'LIBERTREE_DB_BACKEND': 'postgres', 'LIBERTREE_FULL_CHECK_DAYS': '0'}), \
                patch.object(cls, '_request', request), patch.object(cls, '_curl_get', side_effect=AssertionError('unaudited legacy transport')):
            baseline = []
            for label, limit in [('partial', 3), ('resume', None), ('repeat', None)]:
                start = len(calls)
                jobs.enqueue_job(conn, cls.site_id, mode='incremental', limit_n=limit)
                saved = worker.run_job(conn, jobs.claim_next_job(conn), delay=0)
                rows = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(baseline)], baseline); baseline = rows
                state = Store(conn, cls.site_id).state()
                self.assertEqual(state.get('baseline_complete', False), label != 'partial')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'], 'done')
                phases.append(dict(phase=label, saved_new=saved, requests=len(calls)-start, details=0, rows=len(rows)))
            self.assertEqual([p['saved_new'] for p in phases], [3, 13, 0])
            self.assertEqual([p['requests'] for p in phases], [8, 0, 3])
            self.assertEqual(set(papers), {r['post_number'] for r in rows})
            self.assertEqual([r['published_date'] for r in rows], sorted(r['published_date'] for r in rows))
            for row in rows:
                paper = papers[row['post_number']]
                self.assertEqual(row['meta_url'], paper['url'])
                self.assertEqual(row['title'], paper['title'])
                self.assertEqual(row['abstract'], paper['abstract'])
            self.assertEqual(len(state['cursor']['exclusions']), 11)
            self.assertEqual(state['last_run']['save_attempts'], 0)
            self.assertEqual(state['last_run']['stop_reason'], 'known_boundary_plus_overlap')
        output = os.environ.get('INCREMENTAL_ARCHIVES_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed', fixture_transport=True,
                actual_retained_HTTP_bodies=True, synthetic_source_responses=False, live_http_requests=0,
                database=urlsplit(DSN).path[1:], phases=phases, source_memberships=len(manifest),
                native_eligible_documents=16, native_short_description_exclusions=11,
                existing_rows_unchanged=True, native_metadata_fields_equal=True,
                all_source_PDF_bodies_verified=False, global_source_complete=False,
                operating_database_touched=False, source_documents_deleted=False), indent=2)+'\n')


if __name__ == '__main__': unittest.main()
