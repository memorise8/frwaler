"""Native fallback replay with retained listing and controlled HTML/PDF text."""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
HTML=os.environ.get('INCREMENTAL_BMLEH_LIST_HTML')


@unittest.skipUnless(DSN and HTML,'requires a new owned PostgreSQL DB and retained advisory list')
class BmlehWorkerTests(unittest.TestCase):
    def test_default_worker_repeats_all_native_requests_and_never_certifies_global_baseline(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        self.assertTrue(urlsplit(DSN).path.lstrip('/').startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'],'must be a new empty DB')
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS['bmleh-de-en'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        html=Path(HTML).read_text();items=cls(None,delay=0)._parse_list(html);self.assertEqual(len(items),26)
        by_url={r['href']:r for r in items};calls=[];phases=[];body='This is controlled fixture prose used only to measure full-rescan worker persistence and repeated requests. '*4
        def fetch(c,url,*,binary=False,**kw):
            kind='pdf' if binary else 'list' if url==cls._LIST_URL else 'html_detail';calls.append({'url':url,'kind':kind})
            if kind=='list':return html
            if binary:return b'controlled PDF transport fixture; text extraction is separately stubbed'
            item=by_url[url];pdf=(f'<a class="c-download-teaser__a" href="{url}.fixture.pdf">Download</a>' if item['kind']=='detail_pdf' else '')
            return '<html><main id="content"><h1>Fixture publication</h1><p>'+body+'</p>'+pdf+'</main></html>'
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}),patch.object(cls,'_curl',fetch),patch.dict(cls._build_document.__globals__,{'_pdf_text':lambda data:body}),patch.object(cls,'DELIVERY_ORDER','newest_first'):
            baseline=None
            for label in ('initial','repeat'):
                start=len(calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental');saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is not None:self.assertEqual(rows,baseline)
                baseline=rows;state=Store(conn,cls.site_id).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                self.assertIn('nonchronological',state['reason']);self.assertIn('order_review',state)
                phases.append({'phase':label,'saved_new':saved,'rows':len(rows),'transport_calls':len(calls)-start,
                               'list_calls':sum(r['kind']=='list' for r in calls[start:]),
                               'html_detail_calls':sum(r['kind']=='html_detail' for r in calls[start:]),
                               'pdf_calls':sum(r['kind']=='pdf' for r in calls[start:]),'baseline_complete':state['baseline_complete']})
            self.assertEqual([p['saved_new'] for p in phases],[26,0])
            self.assertEqual([p['transport_calls'] for p in phases],[38,38])
            self.assertEqual([p['html_detail_calls'] for p in phases],[16,16])
            self.assertEqual([p['pdf_calls'] for p in phases],[21,21])
        output=os.environ.get('INCREMENTAL_BMLEH_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps({'status':'passed','fixture_transport':True,'live_http_requests':0,
                'database':urlsplit(DSN).path[1:],'retained_source_list_items':26,'html_and_pdf_text_are_synthetic_fixtures':True,
                'phases':phases,'existing_rows_unchanged':True,'actual_pdf_bytes_verified':False,
                'global_source_complete':False,'source_documents_deleted':False},indent=2)+'\n')

if __name__=='__main__':unittest.main()
