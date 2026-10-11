"""Original 102-row list plus explicit synthetic extra-category fixtures."""
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
SOURCE=os.environ.get('INCREMENTAL_AMES_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained Ames source')
class AmesWorkerTests(unittest.TestCase):
    def test_all_native_categories_repeat_and_failure_counts(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_ames_incremental import retained,SITE
        from tests.test_incremental_ames import BASE,BODY,listing,detail
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,items,actual=retained(SOURCE);c=cls(None,delay=0);self.addCleanup(c._session.close)
        responses={p['url']:p['raw'].decode() for p in pages};list_urls=set(responses)
        expected={i['url']:i for i in items}
        for slug,category in cls._CATEGORIES[1:]:
            url=BASE+'/news/controlled-'+slug;item=dict(url=url,title='Controlled '+slug)
            expected[url]=item;responses[c._list_url(slug,0)]=listing([(items[0]['url'],items[0]['title']),(url,item['title'])])
            responses[c._list_url(slug,1)]=listing([]);list_urls.update((c._list_url(slug,0),c._list_url(slug,1)))
        responses.update({u:detail(i['title']) for u,i in expected.items()});responses[items[0]['url']]=actual['raw'].decode()
        calls=[];failure=False;phases=[]
        def curl(cmd,**kwargs):
            self.assertNotIn('-fskIL',cmd);url=cmd[-1];self.assertIn(url,responses);calls.append(url)
            return SimpleNamespace(returncode=22 if failure else 0,stdout=b'' if failure else responses[url].encode(),stderr=b'controlled HTTP503' if failure else b'')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch('time.sleep'), \
                patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for phase in ('initial','repeat','transport_failure'):
                failure=phase=='transport_failure';start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn('fetch_failed',str(dict(job)))
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),
                    native_curl_command_calls=len(reads),list_calls=sum(u in list_urls for u in reads),
                    detail_calls=sum(u not in list_urls for u in reads),job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[104,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[120,120,3])
            self.assertEqual([p['detail_calls'] for p in phases],[104,104,0])
            self.assertEqual(set(expected),{r['meta_url'] for r in rows})
            for row in rows:
                if row['meta_url']==items[0]['url']:
                    self.assertEqual(row['post_number'],actual['paper']['external_id'])
                    self.assertEqual(str(row['published_date']),actual['paper']['published_date'])
                    self.assertEqual(row['abstract'],actual['paper']['abstract'])
                else:
                    self.assertEqual(row['post_number'],c._slug_from_url(row['meta_url']))
                    self.assertIsNone(row['published_date']);self.assertIsNone(row['listed_date'])
                    self.assertEqual(row['abstract'],BODY)
                self.assertEqual(row['title'],expected[row['meta_url']]['title'])
        output=os.environ.get('INCREMENTAL_AMES_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_list_bodies=12,actual_retained_detail_bodies=1,synthetic_detail_bodies=103,
                original_news_release_rows=102,synthetic_extra_category_unique_rows=2,
                extra_categories_not_verified_original_scope=True,synthetic_rows_have_no_invented_dates_or_node_IDs=True,
                request_count_boundary='intercepted native curl command invocations including retries, not redirect hops',
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_unchanged=True,
                baseline_complete=False,global_source_complete=False,all_files_verified=False,
                source_documents_deleted=False,operating_database_touched=False),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main()
