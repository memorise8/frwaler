"""Native fallback request count with real retained list and synthetic PDF IO."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_CELLS_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL DB and retained CELLS source')
class CellsWorkerTests(unittest.TestCase):
    def test_full_rescan_fallback_keeps_all_request_counts_and_unknown_publication_dates(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_cells_incremental import parse
        from tests.test_incremental_cells import CurlFixture
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS['cells-es-en'];db_pg.upsert_site(conn,cls.site_id,cls.site_name,cls.base_url)
        source=Path(SOURCE);prior=json.loads((source/'cells-single-page-independent-review.json').read_text())
        raw=(source/prior['source_file']).read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),prior['source_sha256'])
        c=cls(None,delay=0);self.addCleanup(c._session.close);items,_=parse(c,raw)
        fixture=CurlFixture(raw,[i['pdf_url'] for i in items]);phases=[]
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',fixture), \
                patch.object(cls,'DELIVERY_ORDER','newest_first'), \
                patch('requests.Session.request',side_effect=AssertionError('live network forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            baseline=None
            for label in ('initial','repeat'):
                start=len(fixture.calls);jobs.enqueue_job(conn,cls.site_id,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if baseline is not None:self.assertEqual(rows,baseline)
                baseline=rows;state=Store(conn,cls.site_id).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                self.assertIn('reporting_years_not_publication_dates',state['reason']);self.assertIn('order_review',state)
                calls=fixture.calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    native_HTTP_command_invocations=len(calls),list_calls=sum(c['kind']=='list' for c in calls),
                    pdf_download_calls=sum(c['kind']=='pdf' for c in calls),baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[12,0])
            self.assertEqual([p['native_HTTP_command_invocations'] for p in phases],[13,13])
            self.assertEqual([p['pdf_download_calls'] for p in phases],[12,12])
            expected={i['anchor']:i for i in items};self.assertEqual(set(expected),{r['post_number'] for r in rows})
            for row in rows:
                item=expected[row['post_number']]
                self.assertEqual(row['meta_url'],item['detail_url']);self.assertEqual(row['title'],item['title_raw'])
                self.assertEqual(row['pdf_url'],item['pdf_url']);self.assertEqual(row['original_filename'],item['original_filename'])
                self.assertIsNone(row['published_date']);self.assertIsNone(row['listed_date'])
                self.assertIsNone(row['published_date_raw']);self.assertIsNone(row['listed_date_raw'])
                self.assertIn(item['pdf_url'],row['abstract'])
        output=os.environ.get('INCREMENTAL_CELLS_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                synthetic_PDF_bytes_and_extraction_stdout=True,actual_PDF_text_verified=False,
                request_count_boundary='intercepted native curl subprocess invocations',
                database=urlsplit(DSN).path[1:],phases=phases,retained_source_memberships=12,
                native_listing_fields_preserved=True,fabricated_publication_and_listed_dates_removed=12,
                existing_rows_unchanged=True,global_source_complete=False,all_files_verified=False,
                source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
