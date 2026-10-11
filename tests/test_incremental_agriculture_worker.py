"""Native first page replay with synthetic details/end; not full original source."""
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
SOURCE=os.environ.get('INCREMENTAL_AGRICULTURE_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL DB and retained source')
class AgricultureWorkerTests(unittest.TestCase):
    def test_native_partial_repeat_and_failures_never_certify_original_scope(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_agriculture_incremental import retained,cards
        from tests.test_incremental_agriculture import SITE,BODY,detail
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];g=cls.crawl.__globals__;db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages=retained(SOURCE);raw=pages['exact-native-query-first']['raw'];items=cards(raw)
        self.assertEqual(len(items),4);expected={r['slug']:r for r in items}
        responses={g['_LIST_URL'].format(pg=1):raw.decode(),g['_LIST_URL'].format(pg=2):'<html><main>Controlled empty fixture</main></html>'}
        responses.update({i['url']:detail(i['title']) for i in items})
        calls=[];phase='';phases=[]

        def curl(cmd,**kwargs):
            url=cmd[-1];self.assertIn(url,responses);calls.append(url)
            failure=phase=='list_failure' or phase=='detail_failure' and url in {i['url'] for i in items}
            return SimpleNamespace(returncode=0,stdout=(("Unavailable" if failure else responses[url])+f'\n__HTTP_CODE__:{503 if failure else 200}').encode(),stderr=b'')

        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(g['subprocess'],'run',curl),patch('time.sleep'), \
                patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for phase,limit in [('partial',2),('resume',None),('repeat',None),('list_failure',None),('detail_failure',None)]:
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows[:len(before)],before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                failure=phase.endswith('_failure');self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn('fetch_failed',str(dict(job)))
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),
                    native_curl_command_calls=len(reads),list_calls=sum('espace-presse' in u for u in reads),
                    detail_calls=sum('espace-presse' not in u for u in reads),job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[2,2,0,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[3,6,6,4,5])
            self.assertEqual([p['detail_calls'] for p in phases],[2,4,4,0,4])
            self.assertEqual(set(expected),{r['post_number'] for r in rows})
            for row in rows:
                item=expected[row['post_number']]
                self.assertEqual(row['title'],item['title']);self.assertEqual(row['meta_url'],item['url'])
                self.assertEqual(str(row['published_date']),item['listed_date']);self.assertEqual(str(row['listed_date']),item['listed_date'])
                self.assertEqual(row['pdf_url'],cls.base_url+'/files/study.pdf');self.assertEqual(row['abstract'],BODY.strip())
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM crawl_jobs WHERE status='queued'").fetchone()['n'],0)
        output=os.environ.get('INCREMENTAL_AGRICULTURE_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_first_page=True,synthetic_detail_bodies=4,synthetic_empty_second_page=True,
                request_count_boundary='intercepted native curl subprocess invocations including retries, not wire redirect hops',
                database=urlsplit(DSN).path[1:],phases=phases,native_first_page_fields_preserved=True,
                existing_rows_unchanged=True,baseline_complete=False,global_source_complete=False,all_files_verified=False,
                source_documents_deleted=False,operating_database_touched=False),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main()
