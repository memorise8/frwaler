"""Default worker integration in a fresh owned DB, without deleting documents."""
import os
import unittest
from unittest.mock import Mock,patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires a fresh incremental_rollout_ PostgreSQL DB')
class DSpaceWorkerTests(unittest.TestCase):
    def test_default_worker_merge_resume_repeat(self):
        from crawler import db_pg
        from crawler.sites import CRAWLERS
        from crawler.incremental import Store
        from crawler.incremental_dspace import NemertesAdapter,NEMERTES_SCOPES
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_dspace import item
        self.assertTrue(urlsplit(DSN).path.lstrip('/').startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'],'must be a new empty DB')
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS['nemertes-library-upatras-gr-browse'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        docs={scope:[item(i) for i in range(12,0,-1) if (i-1)%5==index] for index,scope in enumerate(NEMERTES_SCOPES)}
        calls=[]
        def request(c,url,params=None,**kw):
            calls.append((url,params));response=Mock()
            if url.endswith('/bundles'):
                response.json.return_value={'page':{'totalElements':0,'totalPages':0}};return response
            self.assertEqual(params['sort'],'dateaccessioned,desc')
            vals=docs[params['scope']];n=len(vals);page=params['page'];size=params['size']
            response.json.return_value={'page':{'number':page,'size':size,'totalElements':n,'totalPages':(n+size-1)//size},
                                        '_embedded':{'items':vals[page*size:(page+1)*size]}}
            return response
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}),patch.object(NemertesAdapter,'rows',3),patch.object(cls,'_request',request):
            counts=[];request_counts=[];detail_counts=[];baseline=None
            for limit in [3,None,None]:
                start=len(calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental',limit_n=limit)
                counts.append(worker.run_job(conn,jobs.claim_next_job(conn),delay=0));request_counts.append(len(calls)-start)
                detail_counts.append(sum(url.endswith('/bundles') for url,p in calls[start:]))
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is None:
                    baseline=rows
                    self.assertFalse(Store(conn,cls.site_id).state().get('baseline_complete',False))
                else:self.assertEqual(rows[:3],baseline)
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
            self.assertEqual(counts,[3,9,0]);self.assertEqual(detail_counts,[3,9,0])
            self.assertEqual(request_counts[1],9);self.assertEqual(request_counts[2],5)
            self.assertEqual([r['post_number'] for r in rows],[item(i)['uuid'] for i in range(1,13)])
            state=Store(conn,cls.site_id).state();self.assertTrue(state['baseline_complete'])
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(state['last_run']['save_attempts'],0)

if __name__=='__main__':unittest.main()
