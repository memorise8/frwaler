"""Real retained datatables with synthetic HEAD responses through default worker."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_AGRI_SOURCE')
PROBE=os.environ.get('INCREMENTAL_AGRI_PROBE')


@unittest.skipUnless(DSN and SOURCE and PROBE,'requires fresh owned PG, retained source and challenge capture')
class AgriWorkerTests(unittest.TestCase):
    def test_native_repeat_HEAD_failure_and_observed_challenge(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_agri_incremental import SITE,retained
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        raw,items,facts=retained(SOURCE);expected={i['native_slug']:i for i in items}
        probe=Path(PROBE);observed=json.loads((probe/'audit.json').read_text())['requests'][0]
        denied=(probe/observed['body']).read_bytes();self.assertEqual(observed['status'],403)
        self.assertEqual(hashlib.sha256(denied).hexdigest(),observed['sha256'])
        calls=[];phase='';phases=[]

        def listing(c,url,**kwargs):
            self.assertEqual(url,cls.START_URL);calls.append(dict(method='GET',url=url))
            return denied.decode() if phase=='observed_challenge' else raw.decode()

        def head(c,url,**kwargs):
            self.assertIn(url,{r['item_url'] for r in items});calls.append(dict(method='HEAD',url=url))
            return None if phase=='HEAD_failure' else {'content-type':'application/pdf','content-length':'1234'}

        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls,'_curl_text',listing),patch.object(cls,'_curl_headers',head), \
                patch('requests.Session.request',side_effect=AssertionError('live network forbidden')), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',side_effect=AssertionError('live curl forbidden')), \
                patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
            before=None
            for phase in ('initial','repeat','HEAD_failure','observed_challenge'):
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                failure=phase in ('HEAD_failure','observed_challenge')
                self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn('fetch_failed' if phase=='HEAD_failure' else 'authentication_failed',str(dict(job)))
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),
                    native_request_helper_calls=len(reads),list_calls=sum(r['method']=='GET' for r in reads),
                    HEAD_calls=sum(r['method']=='HEAD' for r in reads),detail_GET_calls=0,
                    job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[125,0,0,0])
            self.assertEqual([p['native_request_helper_calls'] for p in phases],[126,126,2,1])
            self.assertEqual([p['HEAD_calls'] for p in phases],[125,125,1,0])
            self.assertEqual(set(expected),{r['post_number'] for r in rows})
            for row in rows:
                item=expected[row['post_number']]
                self.assertEqual(row['title'],item['title']);self.assertEqual(row['pdf_url'],item['pdf_url'])
                self.assertEqual(row['meta_url'],cls.START_URL+'#'+item['section_anchor'])
                self.assertEqual(str(row['published_date']),item['listed_date'])
                self.assertEqual(str(row['listed_date']),item['listed_date'])
                self.assertEqual(row['original_filename'],item['original_filename'])
                self.assertIn(item['item_url'],row['abstract'])
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE status='queued'").fetchone()['n'],0)
        output=os.environ.get('INCREMENTAL_AGRI_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_list_bodies=1,synthetic_HEAD_responses=125,actual_challenge_body_replayed=True,
                request_count_boundary='intercepted _curl_text and _curl_headers helpers, excluding internal retries and redirect hops',
                database=urlsplit(DSN).path[1:],phases=phases,retained_source_memberships=125,
                native_listing_fields_preserved=True,existing_rows_unchanged=True,
                native_abstract_is_generated_metadata_not_document_body=True,
                publication_date_equivalence_unverified=True,baseline_complete=False,
                global_source_complete=False,all_files_verified=False,source_documents_deleted=False,
                operating_database_touched=False),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main()
