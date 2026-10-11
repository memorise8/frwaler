"""Measure bounded native HTML-only fallback without certifying omitted media."""
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
SOURCE=os.environ.get('INCREMENTAL_ANSTO_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ANSTO search responses')
class AnstoWorkerTests(unittest.TestCase):
    def test_bounded_native_repeat_omissions_and_missing_publication_date(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_ansto_incremental import retained,SITE
        from tests.test_incremental_ansto import detail,BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE]
        db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url);pages=retained(SOURCE)
        lists={pages[i]['evidence']['url']:pages[i]['raw'] for i in (0,1)}
        expected={r['url']:r for i in (0,1) for r in pages[i]['items']}
        self.assertEqual(len(expected),28);calls=[];phases=[];failure=False
        def curl(cmd,**kwargs):
            url=cmd[-1];calls.append(url)
            if url in lists:body=lists[url]
            elif url in expected:
                if failure:return SimpleNamespace(returncode=28,stdout=b'',stderr=b'controlled detail failure')
                body=detail().encode()
            else:
                self.assertEqual(url,pages[1]['evidence']['url'].replace('page=1','page=2'))
                return SimpleNamespace(returncode=28,stdout=b'',stderr=b'controlled prefix boundary failure')
            return SimpleNamespace(returncode=0,stdout=body,stderr=b'')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}),patch.object(cls.crawl.__globals__['subprocess'],'run',curl), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=[]
            for phase in ('partial','continue_by_rescan','repeat','detail_failure'):
                failure=phase=='detail_failure';start=len(calls)
                jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=3 if phase=='partial' else None)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(before)],before);before=rows
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'done' if phase=='partial' else 'failed')
                if phase!='partial':self.assertIn('fetch_failed',str(dict(job)))
                state=Store(conn,SITE).state();self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),native_curl_command_calls=len(reads),
                    list_calls=sum(u not in expected for u in reads),detail_calls=sum(u in expected for u in reads),
                    direct_media_calls=0,job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[3,25,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[4,33,33,4])
            self.assertEqual([p['detail_calls'] for p in phases],[3,28,28,3])
            self.assertEqual(set(expected),{r['meta_url'] for r in rows})
            for row in rows:
                self.assertEqual(row['post_number'],row['meta_url'].replace(cls.base_url,'').strip('/'))
                self.assertEqual(row['title'],expected[row['meta_url']]['title']);self.assertEqual(row['abstract'],BODY.strip())
                self.assertIsNone(row['published_date']);self.assertFalse(row['published_date_raw'])
                self.assertIsNone(row['listed_date']);self.assertEqual(row['pdf_url'],cls.base_url+'/sites/default/files/2020-05/fixture.pdf')
        output=os.environ.get('INCREMENTAL_ANSTO_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_AJAX_bodies=2,native_HTML_candidates=28,synthetic_detail_bodies=28,
                omitted_direct_media=12,actual_omitted_PDF_read_by_native=False,actual_detail_content_verified=False,
                synthetic_details_without_node_ID_use_native_slug=True,PDF_path_month_does_not_set_publication_date=True,
                controlled_boundary='page2 transport failure after two retained lists; not source end',
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_preserved=True,
                request_count_boundary='native curl invocations including retries; redirect hops not measured',
                baseline_complete=False,global_source_complete=False,operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
