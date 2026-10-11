"""Nine-scope default worker in a fresh owned PostgreSQL DB; no data deletion."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qsl,urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires a fresh incremental_rollout_ PostgreSQL DB')
class BankWorkerTests(unittest.TestCase):
    def test_nine_scope_default_worker_repeat_and_unresolved_external_hold(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.incremental_bankofcanada import SCOPES
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_incremental_bankofcanada import listing,detail
        self.assertTrue(urlsplit(DSN).path.lstrip('/').startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'],'must be a new empty DB')
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS['bankofcanada-ca-publications'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        scopes=copy.deepcopy(SCOPES)
        for s in scopes:s['rows_per_page']=3
        docs={s['scope']:[i for i in range(27,0,-1) if (i-1)%9==n] for n,s in enumerate(scopes)}
        calls=[];external=False;phases=[]
        def request(c,url,**kw):
            calls.append(url);r=Mock();p=urlsplit(url)
            if '/item-' in p.path:r.text=detail(int(p.path.rstrip('/').rsplit('-',1)[-1]));return r
            pairs=parse_qsl(p.query);filters=[v for k,v in pairs if k.startswith('content_type')]
            scope=next(s for s in scopes if urlsplit(s['archive_url']).path==p.path and [v for k,v in parse_qsl(urlsplit(s['archive_url']).query) if k.startswith('content_type')]==filters)
            ids=docs[scope['scope']];page=int(dict(pairs)['mt_page'])
            r.text=listing(scope,ids[(page-1)*3:page*3],page,(len(ids)+2)//3,dates=scope['scope']!='scope-1',total=len(ids))
            hidden=''.join(f'<input name="content_type[]" value="{v}">' for v in filters)
            r.text=r.text.replace('class="mtw archive">','class="mtw archive">'+hidden)
            if external and scope['scope']=='scope-1':r.text=r.text.replace('https://www.bankofcanada.ca/2026/01/item-28/','https://www.oar-rao.bank-banque-canada.ca/record/1516?ln=en&v=pdf')
            return r
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}),patch('crawler.incremental_bankofcanada.SCOPES',scopes),patch.object(cls,'_request',request):
            baseline=None
            for name,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is None:baseline=rows;self.assertFalse(Store(conn,cls.site_id).state().get('baseline_complete',False))
                else:self.assertEqual(rows[:3],baseline)
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                phases.append({'phase':name,'saved_new':saved,'requests':len(calls)-start,'details':sum('/item-' in u for u in calls[start:]),'rows':len(rows)})
            self.assertEqual([p['saved_new'] for p in phases],[3,24,0])
            self.assertEqual([p['requests'] for p in phases],[14,22,9])
            self.assertEqual([p['details'] for p in phases],[5,22,0])
            self.assertEqual([r['post_number'] for r in rows],[str(i) for i in range(1,28)])
            prior=Store(conn,cls.site_id).state();self.assertEqual(prior['last_run']['stop_reason'],'known_boundary_plus_overlap')
            external=True;docs['scope-1'].insert(0,28);jobs.enqueue_job(conn,cls.site_id,mode='incremental')
            self.assertEqual(worker.run_job(conn,jobs.claim_next_job(conn),delay=0),0)
            state=Store(conn,cls.site_id).state();self.assertEqual(state['support'],'full_rescan_required')
            self.assertIn('external_detail',state['unsupported_reason'])
            self.assertEqual(state['baseline'],prior['baseline']);self.assertEqual(state['last_full_at'],prior['last_full_at'])
            self.assertEqual([dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')],rows)
            self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'failed')
            self.assertFalse(any('oar-rao' in url for url in calls))
        output=os.environ.get('INCREMENTAL_BANK_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps({'status':'passed','fixture_transport':True,'live_http_requests':0,'database':urlsplit(DSN).path[1:],'phases':phases,'external_hold_verified':True,'old_baseline_and_documents_unchanged':True,'no_external_detail_request':True},indent=2)+'\n')

if __name__=='__main__':unittest.main()
