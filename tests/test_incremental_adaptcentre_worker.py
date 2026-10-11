"""Whole retained native catalogue with explicitly synthetic external abstracts."""
import contextlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_ADAPT_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL DB and retained native ADAPT catalogue')
class AdaptCentreWorkerTests(unittest.TestCase):
    def test_all_native_metadata_resume_repeat_request_counts(self):
        from crawler import db_pg
        from crawler.document_dates import normalize_document_dates
        from crawler.incremental import Store
        from crawler.libertree_adapter import paper_to_document
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.scripts.review_adaptcentre_incremental import retained
        from delivery.worker import jobs,worker
        from tests.test_incremental_adaptcentre import Transport,SITE
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        items,pages,hashes=retained(SOURCE);fixture=Transport(items,rows=100);phases=[];papers={}
        original_save=cls._save_paper
        def save(c,paper):
            papers[paper['external_id']]=dict(paper);return original_save(c,paper)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres','LIBERTREE_FULL_CHECK_DAYS':'0'}), \
                patch('requests.Session.request',side_effect=fixture),patch.object(cls,'_save_paper',save), \
                patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
            baseline=None
            for label,limit in [('partial',3),('resume',None),('repeat',None)]:
                start=len(fixture.calls);jobs.enqueue_job(conn,SITE,mode='incremental',limit_n=limit)
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is not None:self.assertEqual(rows[:len(baseline)],baseline)
                baseline=rows;state=Store(conn,SITE).state()
                self.assertEqual(bool(state.get('baseline_complete')),label!='partial')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                calls=fixture.calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    transport_calls=len(calls),list_calls=sum('/wp-json/' in u for u in calls),
                    external_abstract_calls=sum('/wp-json/' not in u for u in calls),
                    baseline_complete=bool(state.get('baseline_complete'))))
            self.assertEqual([p['saved_new'] for p in phases],[3,3496,0])
            self.assertEqual([p['transport_calls'] for p in phases],[73,3496,3])
            self.assertEqual([p['external_abstract_calls'] for p in phases],[3,3496,0])
            self.assertEqual(state['order'],'newest_first');self.assertEqual(state['order_evidence'],'date')
            self.assertEqual(state['last_run']['stop_reason'],'known_boundary_plus_overlap')
            self.assertEqual({r['post_number'] for r in rows},{str(i['id']) for i in items})
            source_by_id={str(i['id']):i for i in items}
            stored_dates=[source_by_id[r['post_number']]['date'] for r in rows]
            self.assertEqual(stored_dates,sorted(stored_dates))
            for row in rows:
                paper=papers[row['post_number']];source=source_by_id[row['post_number']]
                self.assertEqual(row['meta_url'],source['link'])
                doc=paper_to_document(paper);doc=normalize_document_dates(dict(doc,listed_date=doc.get('posted_date')))
                for key in ('title','abstract','authors','published_date','published_date_raw','listed_date','listed_date_raw','pdf_url'):
                    self.assertEqual(row[key] or '',doc[key] or '',(row['post_number'],key))
        output=os.environ.get('INCREMENTAL_ADAPT_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                database=urlsplit(DSN).path[1:],phases=phases,retained_native_metadata=3499,
                source_object_hashes_verified=len(hashes),synthetic_external_abstracts=True,
                synthetic_total_headers_match_current_probe=True,checked_storage_fields=['title','abstract','authors','published_date','published_date_raw','listed_date','listed_date_raw','pdf_url','meta_url','post_number'],
                original_HTML_equivalence_verified=False,external_abstract_identity_verified=False,
                existing_rows_unchanged=True,oldest_CMS_date_first=True,isolated_fixture_baseline_complete=True,
                global_source_complete=False,all_files_verified=False,source_documents_deleted=False,
                operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
