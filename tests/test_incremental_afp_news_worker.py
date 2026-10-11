"""Default all-news adapter in fresh PostgreSQL with controlled transport."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires fresh owned PostgreSQL DB')
class AFPNewsWorkerTests(unittest.TestCase):
    def test_default_scope_full_resume_repeat(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_afp_news import SITE,BASE,Transport
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        fixture=Transport();phases=[]
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}), \
                patch('requests.Session.request',side_effect=fixture),contextlib.redirect_stdout(io.StringIO()):
            before=None
            for label,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(fixture.calls);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows[:len(before)],before)
                before=rows;state=Store(conn,SITE).state()
                self.assertEqual(bool(state.get('baseline_complete')),label!='partial')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                calls=fixture.calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    transport_calls=len(calls),detail_calls=sum('/media-release/' in u for u in calls),
                    baseline_complete=bool(state.get('baseline_complete'))))
            self.assertEqual([p['saved_new'] for p in phases],[3,9,0])
            self.assertEqual([p['transport_calls'] for p in phases],[11,9,3])
            self.assertEqual([p['detail_calls'] for p in phases],[3,9,0])
            self.assertEqual([r['post_number'] for r in rows],[str(1000+i) for i in range(1,13)])
            self.assertEqual(state['order'],'newest_first');self.assertEqual(state['order_evidence'],'date')
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertTrue(all('date_from' not in u and 'date_to' not in u for u in fixture.calls))
            for row in rows:
                i=int(row['post_number'])-1000;self.assertEqual(row['title'],f'Research {i}')
                self.assertEqual(row['meta_url'],BASE+f'/news-centre/media-release/item-{i}')
                self.assertEqual(row['published_date'],f'2026-09-{i:02}')
                self.assertEqual(row['listed_date'],f'2026-09-{i:02}')
        output=os.environ.get('INCREMENTAL_AFP_NEWS_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,synthetic_HTML=True,
                live_http_requests=0,database=urlsplit(DSN).path[1:],phases=phases,
                default_scope_without_date_restriction=True,existing_rows_unchanged=True,
                isolated_fixture_baseline_complete=True,global_source_complete=False,
                source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
