"""Default full fallback: native curl command counts, synthetic source bodies."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires fresh owned PostgreSQL DB')
class AfdWorkerTests(unittest.TestCase):
    def test_full_fallback_resume_and_repeat_do_not_claim_request_savings(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_afd import SITE,BASE,Source
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        source=Source();phases=[]
        def curl(cmd,**kwargs):
            self.assertEqual(cmd[0],'curl')
            return SimpleNamespace(returncode=0,stdout=source(cmd[-1]).encode(),stderr=b'')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch('time.sleep'), \
                patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for label,limit in [('partial',2),('resume',None),('repeat',None)]:
                start=len(source.calls);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows[:len(before)],before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                calls=source.calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    native_curl_command_calls=len(calls),detail_calls=sum('/ressources/' in u for u in calls),
                    list_calls=sum('/publications/liste?' in u for u in calls),baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[2,1,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[3,6,6])
            self.assertEqual([p['detail_calls'] for p in phases],[2,3,3])
            for row in rows:
                i=int(row['post_number'])-1000
                self.assertEqual(row['title'],f'Research {i}');self.assertEqual(row['meta_url'],BASE+f'/fr/ressources/doc-{i}')
                self.assertEqual(row['pdf_url'],BASE+f'/files/doc-{i}.pdf')
                self.assertEqual(row['published_date'],f'2026-09-{i:02}')
                self.assertEqual(row['publisher'],'AFD - Agence Française de Développement')
        output=os.environ.get('INCREMENTAL_AFD_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,synthetic_HTML=True,
                live_http_requests=0,database=urlsplit(DSN).path[1:],phases=phases,
                request_count_boundary='intercepted native curl commands, not wire redirect hops',
                existing_rows_unchanged=True,baseline_complete=False,global_source_complete=False,
                source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
