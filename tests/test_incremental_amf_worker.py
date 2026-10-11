"""Measure native 200-detail cap with retained sitemap in a fresh owned DB."""
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
SOURCE=os.environ.get('INCREMENTAL_AMF_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained AMF source')
class AMFWorkerTests(unittest.TestCase):
    def test_native_cap_repeat_failure_and_missing_date_semantics(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_amf_incremental import retained,SITE
        from tests.test_incremental_amf import detail,BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        raw,entries,actual,facts=retained(SOURCE);all_unique={}
        for u,d in entries:all_unique.setdefault(u.split('?')[0].split('#')[0].rstrip('/'),d)
        expected=dict(list(all_unique.items())[:200]);calls=[];failure=False;phases=[]
        self.assertEqual(len(all_unique),765);self.assertEqual(cls.MAX_PAGES,200)
        def curl(cmd,**kwargs):
            url=cmd[-1];calls.append(url)
            if url==cls.SITEMAP_URL:body=raw
            else:
                self.assertIn(url,expected)
                body=actual[url]['raw'] if url in actual else detail('Controlled '+url.rsplit('/',1)[-1]).encode()
            return SimpleNamespace(returncode=22 if failure else 0,stdout=b'' if failure else body,stderr=b'controlled error')
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
                    native_curl_command_calls=len(reads),sitemap_calls=reads.count(cls.SITEMAP_URL),
                    detail_calls=sum(u!=cls.SITEMAP_URL for u in reads),job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[200,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[201,201,3])
            self.assertEqual([p['detail_calls'] for p in phases],[200,200,0])
            self.assertEqual(set(expected),{r['meta_url'] for r in rows})
            for row in rows:
                url=row['meta_url'];self.assertEqual(str(row['listed_date']),expected[url])
                if url in actual:
                    f=actual[url]['fields'];self.assertEqual(row['post_number'],f['external_id'])
                    self.assertEqual(str(row['published_date']),f['published_date']);self.assertEqual(row['abstract'],f['abstract'])
                    self.assertEqual(row['pdf_url'],f['pdf_url']);self.assertEqual(row['title'],f['title'])
                else:
                    self.assertIsNone(row['published_date']);self.assertEqual(row['post_number'],url.rsplit('/',1)[-1])
                    self.assertEqual(row['abstract'],BODY)
        output=os.environ.get('INCREMENTAL_AMF_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists());real=len(set(expected)&set(actual))
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_sitemap_bodies=1,actual_retained_detail_bodies_used=real,synthetic_detail_bodies=200-real,
                native_candidate_unique_URLs=765,native_cap_attempted_unique_URLs=200,unattempted_native_tail=565,
                synthetic_missing_publication_dates_remain_NULL=True,listed_date_still_native_sitemap_lastmod=True,
                request_count_boundary='native curl invocations including retries, excluding redirect hops',
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_unchanged=True,baseline_complete=False,
                global_source_complete=False,all_files_verified=False,source_documents_deleted=False,
                operating_database_touched=False),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main()
