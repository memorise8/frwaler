"""Real retained OAI metadata, native fallback and a fresh owned PostgreSQL DB."""
import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ARROW_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained Arrow OAI pages')
class ArrowWorkerTests(unittest.TestCase):
    def test_repeat_requests_exclusions_and_protocol_failures(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_arrow_incremental import retained,SITE
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_arrow_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn);cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,papers,facts=retained(SOURCE);expected={p['external_id']:p for p in papers};responses={p['url']:p['raw'] for p in pages}
        ns=cls.crawl.__globals__['_NS'];calls=[];attempts=[];phases=[];phase=None
        loop=ET.fromstring(pages[0]['raw']);request=loop.find('oai:request',ns);request.attrib.clear()
        request.attrib.update(verb='ListRecords',resumptionToken=pages[0]['next_token']);loop_body=ET.tostring(loop)
        error=ET.fromstring(pages[0]['raw']);error.remove(error.find('oai:ListRecords',ns))
        ET.SubElement(error,'{'+ns['oai']+'}error',code='badResumptionToken').text='expired';error_body=ET.tostring(error)
        wrong=ET.fromstring(pages[0]['raw']);wrong.find('oai:request',ns).set('set','publication:other');wrong_body=ET.tostring(wrong)
        def curl(cmd,**kwargs):
            url=cmd[-1];calls.append(url);self.assertIn(url,responses)
            body=error_body if phase=='oai_error' else wrong_body if phase=='wrong_scope' else loop_body if phase=='token_loop' and url==pages[1]['url'] else responses[url]
            return SimpleNamespace(returncode=22 if phase=='fetch_failure' else 0,stdout=body)
        native_save=cls._save_paper
        def save(c,paper):attempts.append(paper['external_id']);return native_save(c,paper)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch.object(cls,'_save_paper',save), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=[]
            for phase in ('partial','continue_by_rescan','repeat','fetch_failure','oai_error','token_loop','wrong_scope'):
                start=len(calls);startsave=len(attempts);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=3 if phase=='partial' else None)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                self.assertEqual(rows[:len(before)],before);before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                failure=phase in ('fetch_failure','oai_error','token_loop','wrong_scope')
                self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn('fetch_failed' if phase=='fetch_failure' else 'invalid_response',str(dict(job)))
                phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),native_curl_calls=len(calls)-start,
                    detail_calls=0,PDF_calls=0,save_attempts=len(attempts)-startsave,job_status=job['status'],baseline_complete=False,
                    stop_condition='protocol_or_transport_failure' if failure else 'new_save_limit' if phase=='partial' else 'terminal_resumption_token'))
            self.assertEqual([p['saved_new'] for p in phases],[3,226,0,0,0,0,0])
            self.assertEqual([p['native_curl_calls'] for p in phases],[1,3,3,3,1,2,1])
            self.assertEqual([p['save_attempts'] for p in phases],[3,229,229,0,0,98,0])
            self.assertEqual(set(expected),{r['post_number'] for r in rows})
            for row in rows:
                paper=expected[row['post_number']]
                # documents has no DOI column; retained() separately compares all
                # native paper fields, including DOI, with the prior capture.
                for column,key in [('title','title'),('abstract','abstract'),('meta_url','url'),('pdf_url','pdf_url'),('original_filename','original_filename')]:
                    self.assertEqual(row[column],paper[key])
                self.assertEqual(str(row['published_date']),paper['published_date']);self.assertEqual(str(row['listed_date']),paper['posted_date'])
        output=os.environ.get('INCREMENTAL_ARROW_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists());Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_retained_OAI_bodies=3,synthetic_document_bodies=0,source_OAI_records=231,native_saved_candidates=229,
                native_abstract_exclusions=2,crosslisted_native_IDs_preserved=True,
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_unchanged=True,known_item_early_stop=False,
                baseline_complete=False,global_source_complete=False,all_files_verified=False,
                operating_database_touched=False,source_documents_deleted=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
