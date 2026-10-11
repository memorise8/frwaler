"""Run in a NEW dedicated PostgreSQL database; never truncate or drop data."""
import json
import os
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit,urlencode

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires a fresh incremental_rollout_ PostgreSQL DB')
class FederalWorkerTests(unittest.TestCase):
    def test_default_worker_preserves_first_saved_rows_across_resume_and_repeat(self):
        from crawler import db_pg
        from crawler.sites import CRAWLERS
        from crawler.incremental import Store
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        self.assertTrue(urlsplit(DSN).path.lstrip('/').startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'], 'must be a new empty DB')
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS['federalregister-gov-documents'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        docs=[{'document_number':str(i),'html_url':f'https://www.federalregister.gov/documents/{i}',
               'title':str(i),'abstract':'', 'publication_date':f'2026-01-{i:02}'} for i in range(1,13)]
        calls=[]
        def request(c,url,params=None,**kw):
            if params:url+='?'+urlencode(params)
            q={k:v[0] for k,v in parse_qs(urlsplit(url).query).items()};calls.append(q)
            values=[d for d in docs if q.get('conditions[publication_date][gte]','0000')<=d['publication_date']<=q.get('conditions[publication_date][lte]','9999')]
            values.sort(key=lambda d:d['publication_date'],reverse=q['order']=='newest')
            n=len(values);page=int(q['page']);size=int(q['per_page']);nxt=None
            if page*size<n:nxt=cls._API_BASE+'?'+urlencode(dict(q,page=page+1,search_after_cursor='fixture'+str(page+1),format='json'))
            response=Mock();response.json.return_value={'count':n,'results':values[(page-1)*size:page*size],'next_page_url':nxt};return response
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}),patch.object(cls,'_PAGE_SIZE',3),patch.object(cls,'_request',request):
            counts=[];request_counts=[];baseline=None
            for limit in [3,None,None]:
                start=len(calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental',limit_n=limit)
                counts.append(worker.run_job(conn,jobs.claim_next_job(conn),delay=0));request_counts.append(len(calls)-start)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is None:
                    baseline=rows
                    self.assertFalse(Store(conn,cls.site_id).state().get('baseline_complete',False))
                else:self.assertEqual(rows[:3],baseline)
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
            self.assertEqual(counts,[3,9,0]);self.assertEqual(request_counts[1],0)
            self.assertLess(request_counts[2],request_counts[0])
            self.assertEqual(len(rows),12)
            state=Store(conn,cls.site_id).state();self.assertTrue(state['baseline_complete'])
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual(state['last_run']['save_attempts'],0)

if __name__=='__main__':unittest.main()
