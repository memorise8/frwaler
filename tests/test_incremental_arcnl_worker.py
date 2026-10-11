"""All retained thesis lists; three real and 94 synthetic detail bodies."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ARCNL_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained ARCNL source')
class ARCNLWorkerTests(unittest.TestCase):
    def test_three_scopes_full_discovery_resume_and_repeat(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_arcnl_incremental import retained,SITE
        from tests.test_incremental_arcnl import response,detail,BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_arcnl_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,items,actual,facts=retained(SOURCE);calls=[];phases=[]
        identities={url:1000000+n for n,url in enumerate(items)}
        def transport(method,url,**kwargs):
            self.assertEqual(method.upper(),'GET');self.assertFalse(kwargs['allow_redirects']);calls.append(url)
            if url in pages:raw=pages[url]
            elif url in actual:raw=actual[url]['raw']
            else:
                self.assertIn(url,items);item=items[url]
                raw=detail(url,item['title'],identity=identities[url],stamp_value=item['listed_date']).encode()
            return response(url,raw.decode())
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}), \
                patch('requests.Session.request',side_effect=transport), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',side_effect=AssertionError('native HTTP forbidden')), \
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
                    list_calls=sum(u in pages for u in reads),detail_calls=sum(u in items for u in reads),
                    baseline_complete=phase!='partial',fixture_baseline=True))
            self.assertEqual([p['saved_new'] for p in phases],[3,94,0])
            self.assertEqual([p['detail_calls'] for p in phases],[3,94,0]);self.assertEqual(phases[1]['list_calls'],0)
            self.assertLess(phases[-1]['list_calls'],phases[0]['list_calls'])
            self.assertEqual(state['order'],'newest_first');self.assertEqual(state['order_evidence'],'date')
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(set(items),{r['meta_url'] for r in rows})
            self.assertEqual([str(r['published_date']) for r in rows],sorted(str(r['published_date']) for r in rows))
            for row in rows:
                item=items[row['meta_url']];self.assertEqual(row['title'],item['title'])
                self.assertEqual(str(row['published_date']),item['listed_date']);self.assertEqual(str(row['listed_date']),item['listed_date'])
                if row['meta_url'] in actual:
                    paper=actual[row['meta_url']]['paper']
                    self.assertEqual(row['post_number'],paper['post_number']);self.assertEqual(row['abstract'],paper['abstract'])
                    self.assertEqual(row['pdf_url'],paper['pdf_url'])
                else:
                    self.assertEqual(row['post_number'],str(identities[row['meta_url']]))
                    self.assertEqual(row['abstract'],BODY);self.assertIsNone(row['pdf_url'])
        output=os.environ.get('INCREMENTAL_ARCNL_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_listing_bodies=14,actual_detail_bodies_used=3,synthetic_detail_bodies=94,
                native_thesis_rows=97,source_order='newest_first',fixture_baseline_complete=True,
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_preserved=True,oldest_first_delivery=True,
                native_three_papers_fields_preserved=True,original_three_thesis_scopes=True,
                global_source_complete=False,all_actual_details_verified=False,all_files_verified=False,
                operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
