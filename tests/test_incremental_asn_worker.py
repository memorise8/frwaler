"""Default two-catalogue worker measured in a new owned PostgreSQL database."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires fresh incremental_rollout_ PostgreSQL DB')
class ASNWorkerTests(unittest.TestCase):
    def test_default_worker_preserves_both_scopes_and_durable_queue(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.incremental_asn import ASNAdapter
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_asn import response_for,identity
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS['asn-fr-information'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        docs={'expertise':[3,2,1],'annual':list(range(12,0,-1))};calls=[];phases=[]
        def request(session,method,url,**kwargs):
            calls.append(url);return response_for(url,docs,**kwargs)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}), \
                patch.object(ASNAdapter,'rows',3),patch('requests.Session.request',request):
            baseline=[]
            for label,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(baseline)],baseline);baseline=rows
                state=Store(conn,cls.site_id).state()
                self.assertEqual(state.get('baseline_complete',False),label!='partial')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                phases.append(dict(phase=label,saved_new=saved,requests=len(calls)-start,
                                   details=sum('/item-' in u for u in calls[start:]),rows=len(rows)))
            self.assertEqual([p['saved_new'] for p in phases],[3,12,0])
            self.assertEqual([p['requests'] for p in phases],[25,0,5])
            self.assertEqual([p['details'] for p in phases],[15,0,0])
            self.assertEqual([r['post_number'] for r in rows],[str(identity('expertise',i)) for i in range(1,4)]+[str(identity('annual',i)) for i in range(1,13)])
            self.assertEqual(rows[0]['listed_date_raw'],'2025-09-01')
            self.assertTrue(state['baseline_complete']);self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(state['last_run']['save_attempts'],0)
        output=os.environ.get('INCREMENTAL_ASN_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                database=urlsplit(DSN).path[1:],phases=phases,default_both_scopes=True,
                existing_rows_unchanged=True,source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
