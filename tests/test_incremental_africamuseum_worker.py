"""99 retained list entries, four real details and 95 explicitly synthetic details."""
import contextlib
from html import escape
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

DSN=os.environ.get('INCREMENTAL_ROLLOUT_TEST_DSN')
SOURCE=os.environ.get('INCREMENTAL_AFRICA_SOURCE')


@unittest.skipUnless(DSN and SOURCE,'requires fresh owned PostgreSQL DB and retained AfricaMuseum sources')
class AfricaMuseumWorkerTests(unittest.TestCase):
    def test_full_repeat_preserves_month_precision_paths_and_real_detail_IDs(self):
        from crawler import db_pg
        from crawler.incremental import Store
        from crawler.sites import CRAWLERS
        from delivery.db.schema import init_delivery_schema
        from delivery.scripts.review_africamuseum_incremental import retained,SITE
        from delivery.worker import jobs,worker
        self.assertTrue(urlsplit(DSN).path[1:].startswith('incremental_rollout_'))
        conn=db_pg.open_db(DSN);self.addCleanup(conn.close)
        self.assertIsNone(conn.execute("SELECT to_regclass('public.documents') AS t").fetchone()['t'])
        db_pg.init_db(conn);init_delivery_schema(conn)
        cls=CRAWLERS[SITE];db_pg.upsert_site(conn,SITE,cls.site_name,cls.base_url)
        pages,items,details=retained(SOURCE);by_url={i[4]:i for i in items};calls=[];phases=[];papers={}
        def curl(cmd,**kwargs):
            self.assertEqual(cmd[0],'curl');url=cmd[-1];p=urlsplit(url);calls.append(url)
            if p.path=='/en/research/news':
                page=int(parse_qs(p.query).get('page',['0'])[0]);raw=pages[page]['raw']
            elif url in details:raw=details[url]['raw']
            else:
                self.assertIn(url,by_url);item=by_url[url]
                # No fabricated Drupal node ID. This fixture exercises the
                # existing slug fallback; actual IDs for these 95 remain unknown.
                raw=('<html><meta property="og:title" content="'+escape(item[1] or item[0],quote=True)
                     +'"><div class="field--name-field-description">'
                     +('Controlled synthetic article body used only for native storage and request-count testing. '*3)
                     +'</div></html>').encode()
            return SimpleNamespace(returncode=0,stdout=raw,stderr=b'')
        original_save=cls._save_paper
        def save(c,paper):
            item=by_url[paper['url']];self.assertEqual(paper['published_date'],item[2])
            self.assertEqual(paper['listed_date'],item[2]);papers[paper['url']]=dict(paper)
            if paper['url'] in details:self.assertEqual(paper,details[paper['url']]['paper'])
            return original_save(c,paper)
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'postgres'}), \
                patch.object(cls.crawl.__globals__['subprocess'],'run',curl),patch.object(cls,'_save_paper',save), \
                patch('time.sleep'),patch('requests.Session.request',side_effect=AssertionError('live network forbidden')), \
                contextlib.redirect_stdout(io.StringIO()):
            before=None
            for label in ('initial','repeat'):
                start=len(calls);jobs.enqueue_job(conn,SITE,mode='incremental')
                saved=worker.run_job(conn,jobs.claim_next_job(conn),delay=0)
                rows=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
                if before is not None:self.assertEqual(rows,before)
                before=rows;state=Store(conn,SITE).state()
                self.assertFalse(state['baseline_complete']);self.assertEqual(state['support'],'full_rescan_required')
                self.assertEqual(conn.execute('SELECT status FROM crawl_jobs ORDER BY id DESC LIMIT 1').fetchone()['status'],'done')
                reads=calls[start:];phases.append(dict(phase=label,saved_new=saved,rows=len(rows),
                    native_curl_command_calls=len(reads),list_calls=sum(urlsplit(u).path=='/en/research/news' for u in reads),
                    detail_calls=sum(urlsplit(u).path!='/en/research/news' for u in reads),baseline_complete=False))
            self.assertEqual([p['saved_new'] for p in phases],[99,0])
            self.assertEqual([p['native_curl_command_calls'] for p in phases],[110,110])
            self.assertEqual([p['detail_calls'] for p in phases],[99,99])
            self.assertEqual({r['meta_url'] for r in rows},set(by_url))
            for row in rows:
                item=by_url[row['meta_url']];paper=papers[row['meta_url']]
                self.assertEqual(row['post_number'],paper['external_id'])
                self.assertEqual(row['title'],paper['title']);self.assertEqual(row['abstract'],paper['abstract'])
                self.assertIsNone(row['published_date']);self.assertIsNone(row['listed_date'])
                self.assertEqual(row['published_date_raw'],item[2]);self.assertEqual(row['listed_date_raw'],item[2])
        output=os.environ.get('INCREMENTAL_AFRICA_WORKER_REPORT')
        if output:
            self.assertFalse(Path(output).exists())
            Path(output).write_text(json.dumps(dict(status='passed',fixture_transport=True,live_http_requests=0,
                database=urlsplit(DSN).path[1:],phases=phases,retained_source_memberships=99,
                actual_retained_list_bodies=11,actual_retained_detail_bodies=4,synthetic_detail_bodies=95,
                actual_other95_Drupal_node_IDs_verified=False,actual_four_native_papers_equal_prior=True,
                all99_month_raw_dates_preserved_without_invented_day=True,all99_original_URLs_preserved=True,
                existing_rows_unchanged=True,request_count_boundary='intercepted native curl commands',
                baseline_complete=False,global_source_complete=False,all_files_verified=False,
                source_documents_deleted=False,operating_database_touched=False),indent=2)+'\n')


if __name__=='__main__':unittest.main()
