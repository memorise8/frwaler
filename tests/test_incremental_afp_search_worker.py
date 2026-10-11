"""Retained whole 53-result search replay, with synthetic detail HTML only."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_AFP_SEARCH_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL DB and retained AFP search pages')
class AfpSearchWorkerTests(unittest.TestCase):
    def test_full_repeat_replays_every_result_and_preserves_missing_source_fields(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.scripts.review_afp_search_incremental import retained,SITE
        from delivery.worker import jobs,worker
        from tests.test_incremental_afp_search import BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,items=retained(SOURCE);expected={i['url']:i for i in items};calls=[];phases=[];papers={}
        def curl(cmd,**kwargs):
            self.assertEqual(cmd[0],'curl');url=cmd[-1];p=urlsplit(url);calls.append(url)
            if p.path=='/search':
                q=parse_qs(p.query);self.assertEqual(q['keys'],['pdf']);self.assertEqual(q['content_type_id'],['All'])
                raw=pages[int(q['page'][0])]['raw']
            else:
                self.assertIn(url,expected);raw=('<html><main><p>'+BODY*4+'</p></main></html>').encode()
            return SimpleNamespace(returncode=0,stdout=raw,stderr=b'')
        original_save=cls._save_paper
        def save(c,p):
            source=expected[p['url']]
            for key,native_key in [('external_id','node_id'),('post_number','node_id'),('title','title'),
                                   ('published_date','listed_date'),('listed_date','listed_date'),('category','category')]:
                self.assertEqual(p[key],source[native_key])
            meta=json.loads(p['metadata']);self.assertEqual(meta['snippet'],source['snippet'])
            self.assertEqual(meta['datetime_raw'],source['datetime_raw']);papers[p['url']]=dict(p)
            return original_save(c,p)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch.object(cls,'_save_paper',save), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live network forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for label in ('initial','repeat'):
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                reads=calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    native_curl_command_calls=len(reads),list_calls=sum(urlsplit(u).path=='/search' for u in reads),
                    detail_calls=sum(urlsplit(u).path!='/search' for u in reads),baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[53,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[59,59])
            self.assertEqual([p['detail_calls'] for p in phases],[53,53])
            self.assertEqual({r['meta_url'] for r in rows},set(expected))
            for row in rows:
                item=expected[row['meta_url']];self.assertEqual(row['post_number'],item['node_id'])
                self.assertEqual(row['title'],item['title']);self.assertIsNone(row['pdf_url'])
                self.assertEqual(row['published_date'],item['listed_date'] or None)
                self.assertEqual(row['listed_date'],item['listed_date'] or None)
                self.assertEqual(row['abstract'],papers[row['meta_url']]['abstract'])
            self.assertEqual(sum(not r['post_number'] and r['published_date'] is None for r in rows),2)
        output=os.environ.get('INCREMENTAL_AFP_SEARCH_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                database=urlsplit(DSN).path[1:],phases=phases,retained_search_results=53,
                actual_retained_list_bodies=True,synthetic_detail_HTML=True,all53_native_listing_fields_preserved=True,
                missing_native_ID_and_date_rows_preserved=2,existing_rows_unchanged=True,
                request_count_boundary='intercepted native curl commands; redirect wire hops not inferred',
                baseline_complete=False,global_source_complete=False,all_files_verified=False,
                source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
