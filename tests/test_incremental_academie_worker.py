"""Hash-verified real HTML/PDF replay through the current default worker."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ACADEMIE_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL DB and retained Academie HTML/PDF')
class AcademieWorkerTests(unittest.TestCase):
    def test_native_full_repeat_measures_all_requests_and_preserves_real_source_fields(self):
        from crawler import db_pg
        from crawler.document_dates import normalize_document_dates
        from crawler.incremental import Store
        from crawler.libertree_adapter import paper_to_document
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_academie_incremental import retained_source,checked,SITE
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        source=Path(SOURCE);pages,papers,fields,responses=retained_source(source)
        expected={r['paper']['url']:r['paper'] for r in papers}
        pdf_review=json.loads((source/'academie-all119-PDF-responses-independent-review.json').read_text())
        native=cls.crawl.__globals__;pdfs={native['_normalize_url'](u):v for u,v in pdf_review['files'].items()}
        required={i['url'] for p in pages for i in p['items'] if i['native'].get('kind','PDF')=='PDF'}
        self.assertEqual(len(required),91)
        for url in required:
            item=pdfs[url];responses[url]=dict(raw=checked(source,item['file'],item['sha256']),kind='PDF',
                                             file=item['file'],sha256=item['sha256'])
        calls,phases=[],[]
        def fetched(c,url,**kwargs):
            response=responses[url];calls.append({'url':url,'kind':response['kind']});return response['raw']
        real_run=subprocess.run
        def pdf_only(cmd,*args,**kwargs):
            self.assertEqual(cmd[:3],['pdftotext','-layout','-'])
            return real_run(cmd,*args,**kwargs)
        original_save=cls._save_paper
        def save(c,paper):
            self.assertEqual(paper,expected[paper['url']]);return original_save(c,paper)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}),patch.object(cls,'_fetch_bytes',fetched), \
                patch.object(cls,'_save_paper',save),patch.object(cls,'DELIVERY_ORDER','newest_first'), \
                patch.object(subprocess,'run',pdf_only), \
                patch('requests.Session.request',side_effect=AssertionError('live network forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for label in ('initial','repeat'):
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                self.assertIn('undated_editorial_sections',state['reason']);self.assertIn('order_review',state)
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                reads=calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    transport_calls=len(reads),list_calls=sum(r['kind']=='list' for r in reads),
                    HTML_detail_calls=sum(r['kind']=='HTML' for r in reads),PDF_calls=sum(r['kind']=='PDF' for r in reads),baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[119,0])
            self.assertEqual([p['transport_calls'] for p in phases],[129,129])
            self.assertEqual([p['list_calls'] for p in phases],[9,9])
            self.assertEqual([p['HTML_detail_calls'] for p in phases],[29,29])
            self.assertEqual([p['PDF_calls'] for p in phases],[91,91])
            self.assertEqual({r['meta_url'] for r in rows},set(expected))
            for row in rows:
                paper=expected[row['meta_url']];doc=paper_to_document(paper)
                doc=normalize_document_dates(dict(doc,listed_date=doc.get('posted_date')))
                self.assertEqual(row['post_number'],paper['external_id'])
                for key in ('title','abstract','pdf_url','original_filename','authors','publisher','keywords',
                            'published_date','published_date_raw','listed_date','listed_date_raw'):
                    self.assertEqual(row[key] or '',doc[key] or '',(row['meta_url'],key))
            self.assertNotIn(fields['exclusions'][0]['item']['url'],{r['meta_url'] for r in rows})
        output=os.environ.get('INCREMENTAL_ACADEMIE_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,
                actual_retained_HTML_and_PDF_bytes=True,synthetic_source_responses=False,
                actual_pdftotext_extraction=True,live_http_requests=0,database=urlsplit(DSN).path[1:],phases=phases,
                retained_source_targets=120,eligible_stored_metadata=119,native_body_exclusions=1,
                source_response_hashes_verified=len(responses),all119_native_paper_and_normalized_storage_fields_equal=True,
                existing_rows_unchanged=True,baseline_complete=False,global_source_complete=False,
                all_current_files_verified=False,source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
