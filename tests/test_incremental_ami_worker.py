"""Count external enrichments separately from database deduplication."""
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
SOURCE=os.environ.get('INCREMENTAL_AMI_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL and retained AMI source')
class AMIWorkerTests(unittest.TestCase):
    def test_repeat_enrichments_year_precision_aliases_and_dependency_failure(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        from delivery.scripts.review_ami_incremental import retained,SITE
        from tests.test_incremental_ami import BODY
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        landing,pages,items,actual=retained(SOURCE)
        lists={cls._LIST_URL:landing,**{cls._LIST_URL+'?year='+p['year']:p['raw'] for p in pages}}
        expected={r['url']:r for r in items};dois={r['doi'] for r in items}
        excluded={d for d,r in actual.items() if not r['prior']['after_count']}
        calls=[];failure=False;phases=[]
        def curl(cmd,**kwargs):
            url=cmd[-1];calls.append(url)
            if url in lists:body=lists[url]
            else:
                self.assertTrue(url.startswith(cls._CROSSREF_BASE+'/'))
                doi=url[len(cls._CROSSREF_BASE)+1:];self.assertIn(doi,dois)
                if failure:return SimpleNamespace(returncode=22,stdout=b'',stderr=b'controlled error')
                body=actual[doi]['raw'] if doi in actual else json.dumps({'message':{'DOI':doi,'abstract':BODY}}).encode()
            return SimpleNamespace(returncode=0,stdout=body,stderr=b'')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch('time.sleep'), \
                patch('requests.Session.request',side_effect=AssertionError('live HTTP forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for phase in ('initial','repeat','crossref_failure'):
                failure=phase=='crossref_failure';start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                job=conn.execute('SELECT * FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()
                self.assertEqual(job['status'],'failed' if failure else 'done')
                if failure:self.assertIn('fetch_failed',str(dict(job)))
                reads=calls[start:];phases.append(dict(phase=phase,saved_new=saved,rows=len(rows),
                    native_curl_command_calls=len(reads),listing_calls=sum(u in lists for u in reads),
                    crossref_calls=sum(u not in lists for u in reads),job_status=job['status'],baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[1371,0,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[1392,1392,5])
            self.assertEqual([p['crossref_calls'] for p in phases],[1372,1372,3])
            self.assertEqual({r['url'] for r in items if r['doi'] not in excluded},{r['meta_url'] for r in rows})
            self.assertEqual(len({r['post_number'] for r in rows}),1369)
            for row in rows:
                item=expected[row['meta_url']];doi=item['doi'];self.assertEqual(row['post_number'],doi)
                self.assertIsNone(row['listed_date']);self.assertEqual(row['listed_date_raw'],item['year'])
                if doi in actual:
                    paper=actual[doi]['prior']['new_papers'][0]
                    self.assertEqual(row['published_date_raw'],paper['published_date'])
                    self.assertEqual(row['abstract'],paper['abstract']);self.assertEqual(row['title'],paper['title'])
                else:
                    self.assertIsNone(row['published_date']);self.assertEqual(row['published_date_raw'],item['year'])
                    self.assertEqual(row['abstract'],BODY.strip())
        output=os.environ.get('INCREMENTAL_AMI_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                actual_listing_bodies=20,actual_Crossref_bodies=3,actual_abstract_eligible_samples=2,
                actual_abstract_exclusions=1,synthetic_unique_Crossref_bodies=1367,synthetic_Crossref_calls_per_run=1369,
                retained_native_URLs=1372,distinct_source_DOIs=1370,stored_unique_DOIs=1369,stored_rows=1371,
                cross_year_DOI_URL_alias_pairs_preserved=2,year_precision_preserved=True,
                request_count_boundary='native curl invocations including retries, excluding redirect hops',
                database=urlsplit(DSN).path[1:],phases=phases,existing_rows_unchanged=True,baseline_complete=False,
                global_source_complete=False,all_actual_enrichments_verified=False,source_documents_deleted=False,
                operating_database_touched=False),ensure_ascii=False,indent=2)+'\n')


if __name__=='__main__':unittest.main()
