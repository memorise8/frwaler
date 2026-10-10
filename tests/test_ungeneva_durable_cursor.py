"""UN source/row anchors, same-slug dated articles and actual worker continuation."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from crawler.base_crawler import CrawlCancelled
from tests import test_europe_press_counts as fixtures
from tests.test_delivery_site_new_counts import InterruptedAfterCommit


class UNGenevaCursorTests(unittest.TestCase):
    BACKEND = 'sqlite'

    def setUp(self):
        self.case = fixtures.UNGenevaCounts(methodName='test_zero_limit_performs_no_fetch')
        self.case.BACKEND = self.BACKEND
        self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.conn = self.case.conn
        self.urls = {n:f'https://www.ungeneva.org/en/news-media/press-release/2025/07/report-{n}' for n in range(1,9)}
        self.dates = {n:'2025-07-11T12:34:56Z' for n in range(1,9)}
        self.excluded = set();self.descriptions = {};self.french = set()

    def card(self, n):
        return (f'<div class="views-row"><a class="un-listings-box" href="{self.urls[n]}">'
                f'<h2 class="item-title">Report {n}</h2><time datetime="{self.dates[n]}"></time>'
                '<span class="item-type">Press Release</span></a></div>')

    def fixture(self, url, **kw):
        if url in self.urls.values():
            n=next(k for k,v in self.urls.items() if v==url)
            body='' if n in self.excluded else 'Original publisher research and publication evidence. '*10
            canonical=url.replace('/en/','/fr/',1) if n in self.french else url
            alternate=f'<link rel="alternate" hreflang="fr" href="{canonical}">' if n in self.french else ''
            return (f'<html><head><title>Report {n} | The United Nations Office at Geneva</title>'
                    f'<link rel="canonical" href="{canonical}">{alternate}'
                    f'<meta property="og:description" content="{self.descriptions.get(n, "")}"></head>'
                    f'<body><h1>Report {n}</h1><time datetime="{self.dates[n]}"></time>'
                    f'<div class="text-long">{body}</div></body></html>')
        page=int(parse_qs(urlsplit(url).query)['page'][0]);ids={0:range(1,6),1:range(6,9)}.get(page,[])
        nxt='<li class="pager__item--next"><a href="?page=1">Next</a></li>' if page==0 else ''
        return '<html><body><h1>Press Releases</h1><div class="news-listing">'+''.join(self.card(n) for n in ids)+nxt+'</div></body></html>'

    def crawler(self, cursor=None):
        c=self.case.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;c._delay=0
        self.case.fetch.side_effect=self.fixture
        return c

    def stored(self):
        rows=self.conn.execute('SELECT seq_id,post_number,meta_url,title,abstract,published_date_raw FROM documents ORDER BY seq_id').fetchall()
        return [tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]

    def test_partial_pages_resume_without_refetching_stored_details(self):
        cursor=None
        for before,new in ((0,3),(3,3),(6,2),(8,0)):
            c=self.crawler(cursor);self.assertEqual(c.crawl(limit=3),new);cursor=c.delivery_pending_cursor
            details=[x.args[0] for x in self.case.fetch.call_args_list if x.args[0] in self.urls.values()]
            self.assertEqual(details,[self.urls[n] for n in range(before+1,before+new+1)])
        self.assertTrue(c.delivery_exhausted);self.assertEqual((cursor['page'],cursor['offset']),(1,3));self.assertEqual(self.case.count(),8)

    def test_cancel_and_commit_before_checkpoint_do_not_lose_or_duplicate_rows(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.case.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],3)
        c=self.crawler(c.delivery_pending_cursor)
        def committed(seq):
            if self.case.count()==5:raise InterruptedAfterCommit()
        c.delivery_document_saved=committed
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],4);before=self.stored()
        c=self.crawler(c.delivery_pending_cursor);self.assertEqual(c.crawl(),3);self.assertTrue(c.delivery_exhausted);self.assertEqual(self.stored()[:5],before)

    def test_identical_slug_in_different_dated_URLs_remains_separate(self):
        self.urls[1]=self.urls[1].replace('report-1','shared-slug');self.urls[6]=self.urls[6].replace('/2025/07/report-6','/2024/07/shared-slug')
        c=self.crawler();self.assertEqual(c.crawl(limit=6),6);before=self.stored();c=self.crawler(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(),2);self.assertEqual(self.stored()[:6],before)
        rows=[r for r in self.stored() if r[1]=='shared-slug'];self.assertEqual(len(rows),2);self.assertNotEqual(rows[0][0],rows[1][0])
        self.assertEqual(self.crawler(c.delivery_pending_cursor).crawl(),0);self.assertEqual(self.case.count(),8)

    def test_short_body_exclusion_is_recorded_and_revalidated(self):
        self.excluded={2};c=self.crawler();self.assertEqual(c.crawl(limit=3),3);cursor=c.delivery_pending_cursor
        self.assertEqual(cursor['offset'],4);self.assertEqual(set(json.loads(cursor['exclusions_json'])),{self.urls[2]})
        c=self.crawler(cursor);self.assertEqual(c.crawl(),4);self.assertTrue(c.delivery_exhausted)
        self.assertIn(self.urls[2],[x.args[0] for x in self.case.fetch.call_args_list]);self.assertNotIn(self.urls[1],[x.args[0] for x in self.case.fetch.call_args_list])

    def test_newly_eligible_or_changed_excluded_body_invalidates_resume(self):
        self.excluded={2};c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for description in ('Video', 'New eligible source summary. '*10):
            self.descriptions[2]=description;c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)
        self.assertEqual(self.case.count(),3)

    def test_tampered_and_extra_exclusion_entries_hold(self):
        self.excluded={2};c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for excluded in ({self.urls[2]:'0'*64},{**json.loads(cursor['exclusions_json']),self.urls[8]:'0'*64}):
            c=self.crawler(dict(cursor,exclusions_json=json.dumps(excluded)));self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_French_declared_canonical_alias_preserves_delivered_URL_and_slug(self):
        self.french={2};c=self.crawler();self.assertEqual(c.crawl(),8)
        row=self.stored()[1];self.assertEqual(row[2],self.urls[2]);self.assertEqual(row[1],'report-2')

    def test_wrong_canonical_undeclared_alias_title_and_date_are_held(self):
        for change in ('canonical','alias','title','date','incomplete','missing_body'):
            c=self.crawler()
            def fetch(url,**kw):
                raw=self.fixture(url,**kw)
                if url==self.urls[4]:
                    if change=='canonical':raw=raw.replace(f'href="{url}"', 'href="https://foreign.invalid/report"')
                    if change=='alias':raw=raw.replace(f'href="{url}"',f'href="{url.replace("/en/","/fr/")}"')
                    if change=='title':raw=raw.replace('<h1>Report 4</h1>','<h1>Other article</h1>')
                    if change=='date':raw=raw.replace('2025-07-11T12:34:56Z','2024-07-11T12:34:56Z')
                    if change=='incomplete':raw=raw.replace('</html>','')
                    if change=='missing_body':raw=raw.replace('class="text-long"','class="wrong-body"')
                return raw
            self.case.fetch.side_effect=fetch;c.crawl();self.assertEqual(self.case.count(),3);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertEqual(c.delivery_pending_cursor['offset'],3);self.assertFalse(c.delivery_exhausted)

    def test_duplicate_membership_allowed_but_changed_duplicate_card_is_held(self):
        c=self.crawler();self.case.fetch.side_effect=lambda url,**kw:self.fixture(url,**kw).replace('<div class="news-listing">','<div class="news-listing">'+self.card(1)) if url.endswith('?page=1') else self.fixture(url,**kw)
        self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted);self.assertEqual(c.delivery_cursor_items_done,8);self.assertEqual(c.delivery_pending_cursor['offset'],4)
        c=self.crawler();self.case.fetch.side_effect=lambda url,**kw:self.fixture(url,**kw).replace('<div class="news-listing">','<div class="news-listing">'+self.card(1).replace('Report 1','Changed card')) if url.endswith('?page=1') else self.fixture(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_previous_page_and_stored_row_changes_reject_resume(self):
        c=self.crawler();c.crawl(limit=6);cursor=c.delivery_pending_cursor
        c=self.crawler(cursor);self.case.fetch.side_effect=lambda url,**kw:self.fixture(url,**kw).replace('Report 1','Changed') if url.endswith('?page=0') else self.fixture(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.conn.execute("UPDATE documents SET abstract='changed source body' WHERE seq_id=1");self.conn.commit();c=self.crawler(cursor)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)

    def test_publisher_and_original_filename_drift_invalidate_resume(self):
        c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        mark='%s' if self.BACKEND=='postgres' else '?'
        for field,changed in (('publisher','Different publisher'),('original_filename','other.pdf')):
            row=self.conn.execute(f'SELECT {field} FROM documents WHERE seq_id=1').fetchone()
            old=row[field] if isinstance(row,dict) else row[0]
            self.conn.execute(f'UPDATE documents SET {field}={mark} WHERE seq_id=1',(changed,));self.conn.commit()
            c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
            self.conn.execute(f'UPDATE documents SET {field}={mark} WHERE seq_id=1',(old,));self.conn.commit()

    def test_file_and_translation_updates_do_not_invalidate_source_anchor(self):
        c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        self.conn.execute("UPDATE documents SET pdf_downloaded=1,text_extracted=1,summary='Independent translation' WHERE seq_id=1");self.conn.commit()
        c=self.crawler(cursor);self.assertEqual(c.crawl(limit=1),1)
        self.conn.execute('DELETE FROM documents WHERE seq_id=1');self.conn.commit();c=self.crawler(cursor);self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_exhausted)

    def test_post_number_change_for_same_URL_holds_without_another_insert(self):
        c=self.crawler();c.crawl(limit=3);self.conn.execute("UPDATE documents SET post_number='changed' WHERE seq_id=1");self.conn.commit()
        c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertEqual(self.case.count(),3)

    def test_valid_empty_boundary_completes_but_empty_error_and_promised_page_do_not(self):
        for raw in ('<html><h1>Error</h1></html>','<html><h1>Press Releases</h1></html>'):
            c=self.crawler();self.case.fetch.side_effect=lambda *a,**kw:raw;self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_exhausted)
        c=self.crawler();self.case.fetch.side_effect=lambda url,**kw:self.fixture(c._LIST_URL+'?page=2') if url.endswith('?page=1') else self.fixture(url,**kw)
        self.assertEqual(c.crawl(),5);self.assertFalse(c.delivery_exhausted)
        c=self.crawler();self.assertEqual(c.crawl(),3);self.assertTrue(c.delivery_exhausted)

    def test_repeated_whole_page_foreign_pager_page_cap_and_wall_hold(self):
        c=self.crawler();self.case.fetch.side_effect=lambda url,**kw:self.fixture(c._LIST_URL+'?page=0') if url.endswith('?page=1') else self.fixture(url,**kw)
        self.assertEqual(c.crawl(),5);self.assertFalse(c.delivery_exhausted)
        c=self.crawler();self.case.fetch.side_effect=lambda url,**kw:self.fixture(url,**kw).replace('href="?page=1"','href="https://foreign.invalid/?page=1"')
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        c=self.crawler();c.MAX_PAGES=1;self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_exhausted)
        c=self.crawler();c.WALL_CLOCK_LIMIT=0;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_failed_detail_does_not_advance_past_failure(self):
        c=self.crawler();self.case.fetch.side_effect=lambda url,**kw:None if url==self.urls[4] else self.fixture(url,**kw)
        self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_pending_cursor['offset'],3);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_invalid_cursor_zero_limit_and_initial_cancel_do_not_fetch(self):
        c=self.crawler({'version':1,'page':False});self.assertEqual(c.crawl(),0);self.case.fetch.assert_not_called()
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.case.fetch.assert_not_called()
        c=self.crawler();c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.case.fetch.assert_not_called()

    def test_raw_invalid_date_is_preserved_without_fabrication(self):
        self.dates[2]='2025-02-30';c=self.crawler();self.assertEqual(c.crawl(),8)
        row=self.conn.execute('SELECT published_date,published_date_raw,published_date_status FROM documents WHERE seq_id=2').fetchone()
        values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
        self.assertEqual(values,(None,'2025-02-30','invalid'))

    def test_incremental_preserves_backfill_checkpoint(self):
        c=self.crawler();c.crawl(limit=3);cursor=c.delivery_pending_cursor;c=self.crawler(cursor);c.delivery_mode='incremental'
        self.assertEqual(c.crawl(),5);self.assertIsNone(c.delivery_pending_cursor);self.assertEqual(cursor['offset'],3)

    def test_transport_checks_HTTP_complete_exit_TLS_and_no_automatic_retry(self):
        # The fixture patch is deliberately absent here: call the actual helper.
        for status,code in ((403,0),(429,0),(503,0),(500,0),(302,0),(200,28)):
            result=SimpleNamespace(stdout=b'<html>source</html>\n'+str(status).encode(),stderr=b'',returncode=code)
            with patch.object(self.case.module.subprocess,'run',return_value=result) as run:
                self.assertIsNone(self.case.module._curl_get('https://www.ungeneva.org/'))
            self.assertEqual(run.call_count,1);cmd=run.call_args.args[0];self.assertNotIn('-sk',cmd);self.assertNotIn('--insecure',cmd);self.assertNotIn('-L',cmd)
        with patch.object(self.case.module.subprocess,'run',return_value=SimpleNamespace(stdout=b'<html>source</html>\n200',stderr=b'',returncode=0)):
            self.assertEqual(self.case.module._curl_get('https://www.ungeneva.org/'),'<html>source</html>')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class UNGenevaCursorPostgresTests(UNGenevaCursorTests):
    BACKEND='postgres'

    def test_worker_automatically_continues_and_completes_after_cancel(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        init_delivery_schema(self.conn);self.excluded={2}
        for index,(limit,cancel,wanted) in enumerate(((3,None,3),(3,2,2),(3,None,2),(None,None,0))):
            c=self.crawler();before=self.case.count()
            if index in (0,2,3):jobs.enqueue_job(self.conn,self.case.SITE,mode='backfill',limit_n=limit)
            job=jobs.claim_next_job(self.conn);self.assertIsNotNone(job)
            def cancelled():
                if cancel and self.case.count()>=before+cancel:jobs.cancel_job(self.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(self.conn,job,{self.case.SITE:lambda **kw:c},delay=0,should_cancel=cancelled),wanted)
            state=jobs.job_detail(self.conn,job['id'])['job'];self.assertEqual((state['saved_count'],state['status']),(wanted,'cancelled' if cancel else 'done'))
        progress=self.conn.execute('SELECT * FROM crawl_site_progress').fetchone();self.assertEqual(progress['items_done'],8);self.assertIsNotNone(progress['completed_at']);self.assertEqual(set(json.loads(progress['cursor']['exclusions_json'])),{self.urls[2]})
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_job_logs WHERE event='stalled'").fetchone()['n'],0)
