"""Bounded native replay: retained first two lists, synthetic PDF extraction only."""
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
SOURCE=os.environ.get('INCREMENTAL_ANSES_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ANSES lists')
class AnsesWorkerTests(unittest.TestCase):
    def test_bounded_prefix_repeat_and_failures_do_not_certify_whole_source(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_anses_incremental import retained,SITE
        from tests.test_incremental_anses import BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE]
        db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url);pages,_=retained(SOURCE)
        c=cls(None,delay=0);self.addCleanup(c._session.close)
        lists={c._list_url(i):pages[key] for i,key in enumerate(('scope-1','scope-1-page-1'))}
        expected={r['pdf_url']:r for p in lists.values() for r in p['items']}
        self.assertEqual(len(expected),20);calls=[];extracts=[];phases=[];pdf_failure=False
        pdf=b'%PDF-1.4 controlled transport placeholder; not an actual publisher PDF'
        def run(cmd,**kwargs):
            if cmd[0]=='pdftotext':
                self.assertEqual(kwargs['input'],pdf);extracts.append(cmd)
                return SimpleNamespace(returncode=0,stdout=BODY.encode(),stderr=b'')
            self.assertEqual(cmd[0],'curl');url=cmd[-1];calls.append(url)
            if url in lists:body=lists[url]['raw'];effective=lists[url]['url']
            elif url in expected:
                if pdf_failure:return SimpleNamespace(returncode=28,stdout=b'',stderr=b'controlled failure')
                body=pdf;effective=url
            else:
                self.assertEqual(url,c._list_url(2))
                return SimpleNamespace(returncode=28,stdout=b'',stderr=b'controlled prefix boundary transport failure')
            return SimpleNamespace(returncode=0,stdout=body+('\n'+c._CURL_META_MARKER+'200\t'+effective).encode(),stderr=b'')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}),patch.object(cls.crawl.__globals__['subprocess'],'run',run), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=[]
            for phase in ('partial','continue_by_rescan','repeat','pdf_failure'):
                pdf_failure=phase=='pdf_failure';start=len(calls);textstart=len(extracts)
                jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=3 if phase=='partial' else None)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(before)],before);before=rows
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'done' if phase=='partial' else 'failed')
                if phase!='partial':self.assertIn('fetch_failed',str(dict(job)))
                state=Store(conn,SITE).state();self.assertFalse(state['baseline_complete'])
                self.assertEqual(state['support'],'full_rescan_required')
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),native_curl_command_calls=len(reads),
                    list_calls=sum(u not in expected for u in reads),pdf_calls=sum(u in expected for u in reads),
                    synthetic_pdftotext_invocations=len(extracts)-textstart,job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[3,17,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[4,25,25,4])
            self.assertEqual([p['pdf_calls'] for p in phases],[3,20,20,3])
            self.assertEqual([p['synthetic_pdftotext_invocations'] for p in phases],[3,20,20,0])
            for row in rows:
                r=expected[row['meta_url']];self.assertEqual(row['post_number'],r['external_id'])
                self.assertEqual(row['title'],r['title']);self.assertEqual(row['published_date_raw'],r['signature_date'] or r['online_date'])
                self.assertEqual(row['listed_date_raw'] or '',r['online_date'])
                self.assertEqual(row['abstract'],BODY.strip())
        output=os.environ.get('INCREMENTAL_ANSES_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_listing_bodies=2,native_original_candidate_URLs=20,synthetic_PDF_transport_bodies=20,
                synthetic_pdftotext_outputs=20,actual_PDF_content_verified=False,original_source_total_verified=False,
                controlled_boundary='page2 transport failure after two retained lists; not source end',
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_preserved=True,
                request_count_boundary='native curl invocations; redirect hops not measured; pdftotext counted separately',
                baseline_complete=False,global_source_complete=False,operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
