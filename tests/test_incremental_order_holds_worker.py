"""Fallback measurements in a new owned PostgreSQL DB; no drop/truncate."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')


@unittest.skipUnless(DSN,'requires a fresh incremental_rollout_ PostgreSQL DB')
class OrderHoldsWorkerTests(unittest.TestCase):
    def test_default_worker_repeats_full_fallback_without_certifying_baseline(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from tests.test_krivet_delivery_scopes import listing,detail
        self.assertTrue(urlsplit(DSN).path.lstrip('/').startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'],'requires a new empty DB')
        db_pg.init_db(conn);init_delivery_schema(conn)
        report={'status':'running','fixture_transport':True,'live_http_requests':0,'database':urlsplit(DSN).path[1:],'sites':{}}
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}):
            for site in ('amolf-nl-publications','krivet-re-kr-kor'):
                cls=CRAWLERS[site];db_pg.upsert_site(conn,site,cls.site_name,cls.base_url)
                calls=[];phases=[];before=None
                def krivet(url,params=None,**kw):
                    calls.append({'url':url,'detail':'pstNo' in params,'menu':params['menuSn']})
                    menu=params['menuSn']
                    if 'pstNo' in params:return detail(menu,params['pstNo'])
                    return listing(menu,[f'{menu}A',f'{menu}B'])
                def amolf(c,url,**kw):
                    calls.append({'url':url,'detail':False})
                    captured=os.environ.get('INCREMENTAL_ORDER_HOLDS_AMOLF_HTML')
                    if captured:return Path(captured).read_text()
                    return '<main><h2><a href="https://www.amolf.nl/publications/item/">Current layout</a></h2></main>'
                transport=(patch.dict(cls.crawl.__globals__,{'_curl_get':krivet}) if site.startswith('krivet')
                           else patch.object(cls,'_curl_get',amolf))
                # Even a legacy newest-first declaration cannot enable unsafe stopping.
                with transport,patch.object(cls,'DELIVERY_ORDER','newest_first'):
                    for phase in ('initial','repeat'):
                        start=len(calls);jobs.enqueue_job(conn,site,mode='incremental')
                        saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                        rows=[dict(r) for r in conn.execute('SELECT * FROM documents WHERE site_id=%s ORDER BY seq_id',(site,))]
                        state=Store(conn,site).state()
                        self.assertEqual(state['support'],'full_rescan_required')
                        self.assertIn('order_review',state)
                        self.assertFalse(state['baseline_complete'])
                        self.assertEqual(state['phase'],'legacy_full_rescan')
                        if before is not None:self.assertEqual(rows,before)
                        before=rows
                        phases.append({'phase':phase,'request_calls':len(calls)-start,
                                       'detail_calls':sum(r['detail'] for r in calls[start:]),
                                       'saved_new':saved,'rows_after':len(rows),'baseline_complete':state['baseline_complete']})
                if site.startswith('krivet'):
                    self.assertEqual([p['saved_new'] for p in phases],[8,0])
                    self.assertEqual([p['request_calls'] for p in phases],[12,12])
                    self.assertEqual([p['detail_calls'] for p in phases],[8,8])
                else:
                    self.assertEqual([p['saved_new'] for p in phases],[0,0])
                    self.assertEqual([p['request_calls'] for p in phases],[1,1])
                report['sites'][site]={'phases':phases,'scope':'four-menu synthetic fixture' if site.startswith('krivet') else 'current layout retained HTML; native parser produces zero rows',
                                      'reason':state['reason'],'existing_rows_unchanged':True,'global_source_complete':False}
        report['status']='passed_default_worker_fallback_measurement'
        output=os.environ.get('INCREMENTAL_ORDER_HOLDS_REPORT')
        if output:
            self.assertFalse(Path(output).exists(),'must preserve previous measurement')
            Path(output).write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')

if __name__=='__main__':unittest.main()
