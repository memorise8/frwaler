"""Owned PostgreSQL integration on all retained reverse-date press pages."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

import requests

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ARCOM_SOURCE')
BATCH=os.environ.get('INCREMENTAL_ARCOM_BATCH')


@unittest.skipUnless(DSN and SOURCE and BATCH,'requires fresh owned PostgreSQL and retained ARCOM press pages')
class ARCOMWorkerTests(unittest.TestCase):
    def test_default_worker_full_discovery_resume_and_repeat(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_arcom_incremental import retained,SITE
        from tests.test_incremental_arcom import detail,BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_arcom_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,expected,actual,facts=retained(SOURCE,BATCH);prior=actual["paper"];calls=[];phases=[]
        def request(session,method,url,**kwargs):
            self.assertEqual(method.upper(),'GET');self.assertFalse(kwargs['allow_redirects']);calls.append(url)
            p=urlsplit(url)
            if p.path=='/presse':raw=pages[int(parse_qs(p.query).get('page',['0'])[0])]['raw']
            else:
                self.assertIn(url,expected);raw=actual['raw'] if url==actual['url'] else detail(expected[url]['title'],url=url,date_raw=expected[url]['date_raw']).encode()
            r=requests.Response();r.url=url;r.status_code=200;r._content=raw;r.encoding='utf-8';return r
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}), \
                patch('requests.Session.request',request),patch.object(cls.crawl.__globals__['subprocess'],'run',side_effect=AssertionError('native transport forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=[]
            for phase,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(before)],before);before=rows
                state=Store(conn,SITE).state();self.assertEqual(state.get('baseline_complete',False),phase!='partial')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),requests=len(reads),
                    detail_calls=sum(u in expected for u in reads),baseline_complete=phase!='partial',fixture_baseline=True))
            self.assertEqual([p['saved_new'] for p in phases],[3,307,0]);self.assertEqual([p['requests'] for p in phases],[17,307,3])
            self.assertEqual([p['detail_calls'] for p in phases],[3,307,0]);self.assertEqual(state['order'],'newest_first')
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(set(expected),{r['meta_url'] for r in rows})
            self.assertEqual([str(r['listed_date']) for r in rows],sorted(str(r['listed_date']) for r in rows))
            for row in rows:
                item=expected[row['meta_url']];self.assertEqual(row['post_number'],item['slug']);self.assertEqual(row['title'],item['title'])
                self.assertEqual(str(row['published_date']),item['date']);self.assertEqual(str(row['listed_date']),item['date'])
                if row['meta_url']==actual['url']:
                    self.assertEqual(row['abstract'],prior['abstract']);self.assertEqual(row['pdf_url'],prior['pdf_url'])
                else:self.assertEqual(row['abstract'],BODY);self.assertIsNone(row['pdf_url'])
        output=os.environ.get('INCREMENTAL_ARCOM_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_listing_bodies=8,actual_detail_bodies_used=1,synthetic_detail_bodies=309,
                native_press_rows=310,source_order='newest_first',fixture_baseline_complete=True,
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_preserved=True,oldest_first_delivery=True,
                native_first_paper_metadata_preserved=True,native_original_slug_and_PDF_preserved=True,
                global_source_complete=False,older_archives_included=False,all_files_verified=False,
                operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
