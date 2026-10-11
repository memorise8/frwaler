"""All retained source lists, synthetic detail bodies, fresh PostgreSQL only."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ALRC_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ALRC catalogues')
class ALRCWorkerTests(unittest.TestCase):
    def test_both_original_scopes_full_pending_resume_repeat(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_alrc_incremental import retained,SITE
        from tests.test_incremental_alrc import response,detail,BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        responses,items,facts=retained(SOURCE);calls=[];phases=[]
        def transport(method,url,**kwargs):
            self.assertEqual(method.upper(),'GET');self.assertFalse(kwargs['allow_redirects']);calls.append(url)
            if url in responses:
                raw,status=responses[url];return response(url,raw.decode(),status)
            self.assertIn(url,items)
            return response(url,detail(url,items[url]['title']))
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}), \
                patch('requests.Session.request',side_effect=transport),contextlib.redirect_stdout(io.StringIO()):
            before=None
            for phase,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows[:len(before)],before)
                before=rows;state=Store(conn,SITE).state()
                self.assertEqual(bool(state.get('baseline_complete')),phase!='partial')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),
                    transport_calls=len(reads),list_calls=sum(u in responses for u in reads),
                    detail_calls=sum(u not in responses for u in reads),baseline_complete=bool(state.get('baseline_complete'))))
            self.assertEqual([p['saved_new'] for p in phases],[3,924,0])
            self.assertEqual([p['detail_calls'] for p in phases],[3,924,0])
            self.assertEqual([p['transport_calls'] for p in phases[:2]],[195,924])
            self.assertLess(phases[-1]['list_calls'],192)
            self.assertEqual(state['order'],'newest_first');self.assertEqual(state['order_evidence'],'date')
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(set(items),{r['meta_url'] for r in rows})
            dates=[items[r['meta_url']]['listed_date'] for r in rows];self.assertEqual(dates,sorted(dates))
            for row in rows:
                item=items[row['meta_url']]
                self.assertEqual(row['post_number'],item['slug']);self.assertEqual(row['title'],item['title'])
                self.assertEqual(str(row['published_date']),item['listed_date']);self.assertEqual(str(row['listed_date']),item['listed_date'])
                self.assertEqual(row['abstract'],BODY);self.assertIsNone(row['pdf_url'])
                self.assertEqual(row['published_date_raw'],item['listed_date_raw'])
                self.assertEqual(row['listed_date_raw'],item['listed_date_raw'])
            self.assertEqual(len({r['post_number'] for r in rows}),913)
        output=os.environ.get('INCREMENTAL_ALRC_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_list_responses=98,synthetic_detail_bodies=927,actual_native_detail_IDs_verified=False,
                native_slug_fallback_preserved=True,unique_fallback_slugs=913,distinct_original_URLs=927,
                database=urlsplit(DSN).path[1:],phases=phases,oldest_date_first=True,default_both_delivered_scopes=True,
                existing_rows_unchanged=True,isolated_fixture_baseline_complete=True,global_source_complete=False,
                all_files_verified=False,source_documents_deleted=False,operating_database_touched=False),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main()
