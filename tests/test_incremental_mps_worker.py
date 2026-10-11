"""Replay native first-page identities and two actual detail samples in an owned DB."""
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
SOURCE=os.environ.get('INCREMENTAL_MPS_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained MPS API bodies')
class MPSWorkerTests(unittest.TestCase):
    def test_native_prefix_repeat_and_ID_mismatch_do_not_certify_completion(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_mps_incremental import retained,SITE
        from tests.test_incremental_mps import BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,_=retained(SOURCE);first=pages['exact-native-unfiltered-first'];g=cls.crawl.__globals__
        c=cls(None,delay=0);self.addCleanup(c._session.close)
        expected={str(r['id']):r for r in first['items']};detail_urls={c._detail_url(id):id for id in expected}
        actual={str(p['items'][0]['id']):p for k,p in pages.items() if 'detail' in k and str(p['items'][0]['id']) in expected}
        self.assertEqual(len(actual),2);calls=[];phases=[];mismatch=False
        def curl(cmd,**kwargs):
            url=cmd[-1];calls.append(url)
            if url==c._list_url(1):raw=first['raw'].decode()
            elif url in detail_urls:
                id=detail_urls[url]
                if mismatch:raw=json.dumps({'resultMap':[{'id':'wrong','htmlContent':BODY}]})
                elif id in actual:raw=actual[id]['raw'].decode()
                else:raw=json.dumps({'resultMap':[{**expected[id],'htmlContent':BODY}]},ensure_ascii=False)
            else:
                self.assertEqual(url,c._list_url(2))
                return SimpleNamespace(returncode=28,stdout='',stderr='controlled prefix boundary failure')
            return SimpleNamespace(returncode=0,stdout=raw,stderr='')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}),patch.object(g['subprocess'],'run',curl), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=[]
            for phase in ('partial','continue_by_rescan','repeat','wrong_detail_ID'):
                mismatch=phase=='wrong_detail_ID';start=len(calls)
                jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=3 if phase=='partial' else None)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(before)],before);before=rows
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'done' if phase=='partial' else 'failed')
                if phase!='partial':self.assertIn('invalid_response' if mismatch else 'fetch_failed',str(dict(job)))
                state=Store(conn,SITE).state();self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),native_curl_command_calls=len(reads),
                    list_calls=sum(u not in detail_urls for u in reads),detail_calls=sum(u in detail_urls for u in reads),
                    job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[3,12,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[4,19,19,2])
            self.assertEqual([p['detail_calls'] for p in phases],[3,15,15,1])
            self.assertEqual(set(expected),{r['post_number'] for r in rows})
            for row in rows:
                id=row['post_number'];d=actual[id]['items'][0] if id in actual else expected[id]
                self.assertEqual(row['meta_url'],g['_DETAIL_URL_TPL'].format(id));self.assertEqual(row['title'],d['title'])
                self.assertEqual(row['published_date_raw'],g['_parse_date'](d.get('scrq') or '') or g['_parse_date'](d.get('publishTime') or ''))
                self.assertEqual(row['abstract'],g['_strip_tags'](d['htmlContent']) if id in actual else BODY.strip())
                self.assertIsNone(row['pdf_url'])
        output=os.environ.get('INCREMENTAL_MPS_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_native_list_bodies=1,actual_detail_bodies_used=2,synthetic_detail_bodies=13,native_prefix_rows=15,
                attachment_fields_not_downloaded=True,native_pdf_url_still_NULL=True,wrong_detail_ID_not_saved=True,
                controlled_boundary='page2 transport failure after retained first list; not source end',
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_preserved=True,
                request_count_boundary='native curl subprocess invocations including retries',baseline_complete=False,
                global_source_complete=False,all_files_verified=False,operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
