"""Native single-list fallback, real retained HTML and a fresh owned database."""
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
SOURCE=os.environ.get('INCREMENTAL_ARN_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ARN evidence')
class ARNWorkerTests(unittest.TestCase):
    def test_rescan_limits_existing_rows_and_failed_source(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_arn_incremental import retained,SITE
        from tests.test_incremental_accc import DENIAL
        from tests.test_incremental_arn import listing
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_arn_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,papers,facts=retained(SOURCE);raw=next(p['raw'] for p in pages if p['url']==cls._LIST_URL)
        expected={p['post_number']:p for p in papers};calls=[];phases=[];phase=None;attempts=[]
        def curl(cmd,**kwargs):
            self.assertEqual(cmd[0],'curl');self.assertEqual(cmd[-1],cls._LIST_URL);calls.append(cmd[-1])
            body=DENIAL.encode() if phase=='challenge' else listing([]).encode() if phase=='empty' else raw
            return SimpleNamespace(returncode=22 if phase=='fetch_failure' else 0,stdout=body)
        native_save=cls._save_paper
        def save(c,paper):attempts.append(paper['post_number']);return native_save(c,paper)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch.object(cls,'_save_paper',save), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=[]
            for phase in ('partial','continue_by_rescan','repeat','fetch_failure','challenge','empty'):
                start=len(calls);startsave=len(attempts);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=3 if phase=='partial' else None)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(before)],before);before=rows
                state=Store(conn,SITE).state();self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                failure={'fetch_failure':'fetch_failed','challenge':'authentication_failed','empty':'invalid_response'}.get(phase)
                self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn(failure,str(dict(job)))
                phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),native_curl_calls=len(calls)-start,
                    detail_calls=0,PDF_calls=0,save_attempts=len(attempts)-startsave,job_status=job['status'],baseline_complete=False,
                    stop_condition=failure or ('new_save_limit' if phase=='partial' else 'native_single_page_all_links_processed')))
            self.assertEqual([p['saved_new'] for p in phases],[3,9,0,0,0,0]);self.assertEqual([p['native_curl_calls'] for p in phases],[1,1,1,3,1,1])
            self.assertEqual([p['save_attempts'] for p in phases],[3,12,12,0,0,0])
            self.assertEqual(set(expected),{r['post_number'] for r in rows})
            for row in rows:
                paper=expected[row['post_number']]
                for column,key in [('title','title'),('abstract','abstract'),('meta_url','url'),('pdf_url','pdf_url'),('original_filename','original_filename')]:
                    self.assertEqual(row[column],paper[key])
                self.assertIsNone(row['published_date']);self.assertIsNone(row['listed_date'])
                self.assertIsNone(row['published_date_raw']);self.assertIsNone(row['listed_date_raw'])
        output=os.environ.get('INCREMENTAL_ARN_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists());Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_annual_HTML_bodies=1,synthetic_document_bodies=0,native_constructed_abstract_not_PDF_text=True,
                annual_metadata_records=12,database=urlsplit(DSN).path[1:],phases=phases,existing_rows_unchanged=True,
                per_report_publication_dates_unknown=True,known_item_early_stop=False,other_three_scopes_configured=False,
                baseline_complete=False,global_source_complete=False,all_files_verified=False,
                operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
