"""Observed ADR listing/report shapes plus actual SQLite/PostgreSQL lifecycle."""
import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock, patch

from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract

BODY = 'Administrative data research explains education, health and public services. ' * 3
ROOT = 'https://www.adruk.org'
ANNUAL = 'https://reports.adruk.org/annual-report-2025-2026/'
ANNUAL_PDF = 'https://reports.adruk.org/fileadmin/Annual_Report_2025-26.pdf'


def listing(scope, page, *, total=4, replacement=None):
    ids = ['I1','I2','I3','I4'] if scope=='impact' else ['P5','P6','P7.pdf','P8.pdf']
    ids = ids[page*2:page*2+2] if total else []
    if replacement:ids = [replacement if x=='I2' else x for x in ids]
    model = 'model-impact-cases' if scope=='impact' else 'model-publications-amp-amp-reports'
    prefix = '/our-mission/our-impact/article/' if scope=='impact' else '/news-publications/publications-reports/publication/'
    records=[]
    for uid in ids:
        href='/fileadmin/'+uid if uid.endswith('.pdf') else prefix+uid+'/'
        title='ADR UK Annual Report 2025 - 2026' if uid=='P5' else 'Research '+uid
        body='' if uid=='P8.pdf' else BODY
        records.append(f'<div class="record {model}"><a href="{href}"><h2>{title}</h2><span class="date">30 September 2026</span><p>{body}</p><p class="fauxLink">Read more</p></a></div>')
    first=page*2+1 if total else 0;last=min((page+1)*2,total)
    path='/our-mission/our-impact/' if scope=='impact' else '/news-publications/publications-reports/'
    next_link=f'<a href="{path}{page+1}/">Next »</a>' if last<total else ''
    return f'<p>Displaying results {first} to {last} out of {total}</p>'+''.join(records)+next_link


def detail(uid, *, date='30 September 2026', body=BODY, linked=True):
    scope='impact' if uid.startswith('I') else 'publications'
    model='model-impact-cases' if scope=='impact' else 'model-publications-amp-amp-reports'
    title='ADR UK Annual Report 2025 - 2026' if uid=='P5' else 'Research '+uid
    report=(f'<a href="{ANNUAL}">Explore the annual report</a>' if linked else '<a href="/fileadmin/Annual_Report_2025-26.pdf">Download report</a>') if uid=='P5' else ''
    return f'<h1><span>{title}</span></h1><div class="news single {model}"><span class="date">{date}</span><p class="category"><a>Research</a></p><div class="content"><p>{body}</p>{report}</div></div>'


class AdrukScopes(SiteContract, unittest.TestCase):
    SITE='adruk-org-news-publications'

    def crawler(self):
        c=self.configured(self.module.AdrukOrgNewsPublicationsCrawler(self.conn));self.addCleanup(c._session.close)
        def fetch(url,**kwargs):
            if url==self.module._ANNUAL_INDEX:return f'<main><h1>Annual Reports</h1><a href="{ANNUAL}">Annual Report 2025-2026</a></main>'
            if url==ANNUAL:return f'<main><h1>Annual Report 2025-2026</h1><a href="{ANNUAL_PDF}">ADR UK Annual Report 2025 - 2026</a><a href="/fileadmin/strategy2025.pdf">Unrelated strategy</a></main>'
            for scope in self.module._SCOPES:
                for page in (0,1):
                    if url==self.module._list_url(scope,page):return listing(scope,page)
            if '/article/' in url or '/publication/' in url:return detail(url.rstrip('/').rsplit('/',1)[-1],date=getattr(self,'source_date','30 September 2026'))
            raise AssertionError('Unexpected request: '+url)
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch))
        self.pdf_body=Mock(return_value=(BODY,{'abstract_source':'fixture_verified_pdf_text'}));c._pdf_body=self.pdf_body
        return c

    def test_all_three_entry_routes_direct_pdfs_and_annual_primary(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)
        urls=[x.args[0] for x in self.fetch.call_args_list]
        for url in (self.module._ANNUAL_INDEX,self.module._list_url('impact',0),self.module._list_url('publications',0)):
            self.assertIn(url,urls)
        self.assertNotIn(ROOT+'/fileadmin/P7.pdf',urls)
        self.pdf_body.assert_called_once_with(ROOT+'/fileadmin/P8.pdf')
        row=self.conn.execute("SELECT pdf_url FROM documents WHERE post_number='P5'").fetchone()
        self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],ANNUAL_PDF)

    def test_month_precision_and_raw_source_date_are_preserved(self):
        self.source_date='September 2026';c=self.crawler();c.crawl(limit=1)
        row=self.conn.execute('SELECT published_date_raw,published_date_precision,listed_date_raw FROM documents').fetchone()
        values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
        self.assertEqual(values,('September 2026','month','30 September 2026'))

    def test_bad_date_suffix_is_not_silently_truncated(self):
        self.source_date='30 September 2026 uncertain';c=self.crawler();c.crawl(limit=1)
        row=self.conn.execute('SELECT published_date_raw,published_date_status FROM documents').fetchone()
        self.assertEqual(tuple(row.values()) if isinstance(row,dict) else tuple(row),('30 September 2026 uncertain','unrecognized'))

    def test_cancel_before_network_and_after_save(self):
        c=self.crawler();c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.fetch.assert_not_called();c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        before=self.identities();self.assertEqual(self.crawler().crawl(),5)
        self.assertTrue(all(self.identities()[k]==v for k,v in before.items()))

    def test_zero_limit_has_no_requests(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()

    def test_denial_and_error_html_are_not_empty_completion(self):
        for error,reason in ((PermissionError('HTTP 403'),'authentication_failed'),(PermissionError('HTTP 429'),'rate_limited'),(None,'fetch_failed'),('<h1>Unavailable</h1>','invalid_response')):
            c=self.crawler()
            if isinstance(error,Exception):self.fetch.side_effect=error
            else:self.fetch.side_effect=lambda *a,**kw:error
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,reason);self.assertFalse(c.delivery_exhausted)

    def test_missing_annual_reference_is_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:detail('P5',linked=False) if url.endswith('/P5/') else original(url,**kw)
        c._same_pdf=lambda a,b:False
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

    def test_two_host_annual_pdf_identity_is_compared_not_filename_only(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:detail('P5',linked=False) if url.endswith('/P5/') else original(url,**kw)
        c._pdf_bytes=Mock(return_value=b'%PDF-1.7\nSame verified source bytes')
        self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)
        self.assertEqual(c._pdf_bytes.call_count,2)
        self.assertEqual({x.args[0] for x in c._pdf_bytes.call_args_list},{ROOT+'/fileadmin/Annual_Report_2025-26.pdf',ANNUAL_PDF})

    def test_existing_identity_collision_does_not_modify_or_merge_rows(self):
        c=self.crawler();c.crawl(limit=1)
        original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw).replace('/article/I1/','/article/nested/I1/')
        self.assertEqual(c.crawl(limit=1),1);self.assertEqual(c.delivery_source_failure,'item_failed')
        row=self.conn.execute("SELECT meta_url FROM documents WHERE post_number='I1'").fetchone()
        self.assertEqual(row['meta_url'] if isinstance(row,dict) else row[0],ROOT+'/our-mission/our-impact/article/I1/')

    def test_repeated_page_or_changed_total_is_incomplete(self):
        for changed in (False,True):
            c=self.crawler();original=self.fetch.side_effect
            def fetch(url,**kw):
                if url==self.module._list_url('impact',1):return listing('impact',0) if not changed else listing('impact',1,total=5)
                return original(url,**kw)
            self.fetch.side_effect=fetch;c.crawl();self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
            self.conn.execute('DELETE FROM documents');self.conn.commit()

    def test_no_200_page_cap_when_source_next_ranges_advance(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,**kw):
            if url.endswith('/I1/'):return detail('I1')
            if url==self.module._list_url('impact',0) or ('/our-impact/' in url and '/article/' not in url):
                page=0 if url==self.module._list_url('impact',0) else int(url.rstrip('/').rsplit('/',1)[-1]);n=page+1
                next_link=f'<a href="{self.module._list_url("impact",n)}">Next »</a>' if n<201 else ''
                return f'<p>Displaying results {n} to {n} out of 201</p><div class="record model-impact-cases"><a href="/our-mission/our-impact/article/I1/"><h2>Research I1</h2><p>{BODY}</p></a></div>'+next_link
            return original(url,**kw)
        self.fetch.side_effect=fetch
        self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)
        self.assertIn(self.module._list_url('impact',200),[x.args[0] for x in self.fetch.call_args_list])


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AdrukScopesPostgres(AdrukScopes):BACKEND='postgres'


class AdrukCursor(AdrukScopes):
    def backfill(self,cursor=None):
        c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;return c

    def test_cursor_resumes_across_both_lists_and_annual_reconciliation(self):
        cursor=None
        for expected in (3,3,2):
            c=self.backfill(cursor);self.assertEqual(c.crawl(limit=3),expected);cursor=json.loads(json.dumps(c.delivery_pending_cursor))
            self.assertTrue(all(not isinstance(v,(dict,list)) for v in cursor.values()))
        self.assertTrue(c.delivery_exhausted);self.assertEqual(self.count(),8)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_exhausted)

    def test_cancel_preserves_last_completed_offset(self):
        c=self.backfill();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        cursor=c.delivery_pending_cursor;self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),('impact',1,1))
        c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)

    def test_body_and_save_gaps_hold_offset(self):
        for failure in ('body','save'):
            c=self.backfill();original=c._save_paper
            if failure=='body':c._pdf_body=lambda u:('',{})
            else:
                def save(paper):
                    if paper['external_id']=='P8.pdf':raise RuntimeError('save failed')
                    return original(paper)
                c._save_paper=save
            self.assertEqual(c.crawl(),7);self.assertFalse(c.delivery_exhausted);self.assertEqual(c.delivery_source_failure,'item_failed')
            cursor=c.delivery_pending_cursor;self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),('publications',1,1))
            c=self.backfill(cursor);self.assertEqual(c.crawl(),1);self.assertTrue(c.delivery_exhausted)
            self.conn.execute('DELETE FROM documents');self.conn.commit()

    def test_previous_current_and_annual_source_drift_are_rejected(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for kind in ('previous','current','annual','total','empty'):
            c=self.backfill(cursor);original=self.fetch.side_effect
            def fetch(url,**kw):
                raw=original(url,**kw)
                if kind=='annual' and url==ANNUAL:return raw.replace('Annual_Report_2025-26.pdf','Annual_Report_2025-26-v2.pdf')
                if kind=='previous' and url==self.module._list_url('impact',0):return raw.replace('I2','changed')
                if url==self.module._list_url('impact',1):
                    if kind=='current':return raw.replace('I4','changed')
                    if kind=='total':return raw.replace('out of 4','out of 5')
                    if kind=='empty':return listing('impact',0,total=0)
                return raw
            self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted);self.assertIsNone(c.delivery_pending_cursor)

    def test_malformed_cursor_is_rejected_before_network(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for fields in ({'contract':'bad'},{'page':False},{'offset':-1},{'annual_matched':'{}'},{'page_hash':''}):
            c=self.backfill(dict(cursor,**fields));self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration');self.fetch.assert_not_called()

    def test_wall_budget_and_full_mode_rescan(self):
        c=self.backfill()
        with patch.object(self.module,'_WALL_CLOCK_LIMIT',.5),patch.object(self.module.time,'monotonic',side_effect=lambda:1 if self.count()>=3 else 0):
            self.assertEqual(c.crawl(),3)
        self.assertFalse(c.delivery_source_failure);cursor=c.delivery_pending_cursor
        c=self.crawler();c.delivery_cursor=cursor;self.assertEqual(c.crawl(),5);self.assertIsNone(c.delivery_pending_cursor)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AdrukCursorPostgres(AdrukCursor):BACKEND='postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AdrukCursorWorker(unittest.TestCase):
    def test_actual_worker_cancel_resume_and_auto_chain(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs, worker
        case=AdrukCursorPostgres(methodName='test_cursor_resumes_across_both_lists_and_annual_reconciliation')
        try:
            case.setUp();init_delivery_schema(case.conn)
            class Factory:
                DELIVERY_CURSOR_VERSION=case.module.AdrukOrgNewsPublicationsCrawler.DELIVERY_CURSOR_VERSION
                DELIVERY_ORDER='arbitrary'
                def __new__(cls,**kwargs):return case.crawler()
            registry={case.SITE:Factory}
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),registry,delay=0),3)
            self.assertEqual(jobs.load_cursor(case.conn,case.SITE)['offset'],1)
            job=jobs.claim_next_job(case.conn);before=case.identities()
            def cancel():
                if case.count()>=len(before)+1:
                    jobs.cancel_job(case.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(case.conn,job,registry,delay=0,should_cancel=cancel),1)
            self.assertEqual(jobs.job_detail(case.conn,job['id'])['job']['status'],'cancelled')
            self.assertIsNone(jobs.claim_next_job(case.conn))
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            counts=[]
            for _ in range(8):
                job=jobs.claim_next_job(case.conn)
                if not job:break
                counts.append(worker.run_job(case.conn,job,registry,delay=0))
            self.assertEqual(counts,[3,1]);self.assertEqual(case.count(),8)
            self.assertTrue(all(case.identities()[key]==value for key,value in before.items()))
            progress=case.conn.execute('SELECT * FROM crawl_site_progress WHERE site_id=%s',(case.SITE,)).fetchone()
            self.assertIsNotNone(progress['completed_at'])
        finally:case.doCleanups()


class AdrukTransport(unittest.TestCase):
    def setUp(self):
        import importlib.util
        f=Path(__file__).resolve().parents[1]/'crawler/sites/custom/adruk-org-news-publications.py';spec=importlib.util.spec_from_file_location('adruk_transport',f);self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)

    def test_denial_does_not_retry_or_disable_tls(self):
        with patch.object(self.m.subprocess,'run',return_value=Mock(returncode=0,stdout=b'denied\n403')) as run:
            with self.assertRaises(PermissionError):self.m._curl_get(ROOT)
        self.assertEqual(run.call_count,1);self.assertNotIn('-skL',run.call_args.args[0])

    def test_http200_body_is_preserved_without_status_trailer(self):
        with patch.object(self.m.subprocess,'run',return_value=Mock(returncode=0,stdout=b'<p>Source</p>\n200')):
            self.assertEqual(self.m._curl_get(ROOT),'<p>Source</p>')

    def pdf_crawler(self, responses):
        c=self.m.AdrukOrgNewsPublicationsCrawler(None)
        self.addCleanup(c._session.close)
        c._session.get=Mock(side_effect=responses)
        return c

    @staticmethod
    def response(status=200,body=b'%PDF-1.7\nsource',headers=None):
        r=Mock(status_code=status,headers=headers or {})
        r.__enter__=Mock(return_value=r);r.__exit__=Mock(return_value=False)
        r.iter_content=Mock(return_value=iter([body]));return r

    def test_pdf_redirect_scope_and_incomplete_file_are_rejected(self):
        cases=[
            ([self.response(302,headers={'Location':'https://example.org/fileadmin/a.pdf'})],ValueError),
            ([self.response(headers={'Content-Length':'100'})],ValueError),
            ([self.response(body=b'<html>Error</html>')],ValueError),
            ([self.response(403)],PermissionError),
            ([self.response(429)],PermissionError),
        ]
        for responses,error in cases:
            c=self.pdf_crawler(responses)
            with self.assertRaises(error):c._pdf_bytes(ROOT+'/fileadmin/a.pdf')
            self.assertEqual(c._session.get.call_count,1)

    def test_publisher_pdf_redirect_and_exact_length(self):
        body=b'%PDF-1.7\nsource'
        c=self.pdf_crawler([self.response(302,headers={'Location':ANNUAL_PDF}),self.response(body=body,headers={'Content-Length':str(len(body))})])
        self.assertEqual(c._pdf_bytes(ROOT+'/fileadmin/a.pdf'),body)
        self.assertEqual(c._session.get.call_args.args[0],ANNUAL_PDF)

    def test_pdf_download_cancellation_propagates(self):
        c=self.pdf_crawler([self.response()]);calls=[]
        def cancel():
            calls.append(1);return len(calls)>1
        c.delivery_should_cancel=cancel
        with self.assertRaises(CrawlCancelled):c._pdf_bytes(ROOT+'/fileadmin/a.pdf')

    def test_pdf_text_failure_does_not_create_fallback_body(self):
        c=self.pdf_crawler([]);c._pdf_bytes=lambda url:b'%PDF-1.7\nsource'
        with patch('delivery.files.worker.run_stage',return_value={'success':False,'error':'pdf_copy_restricted'}):
            with self.assertRaisesRegex(ValueError,'pdf_copy_restricted'):c._pdf_body(ANNUAL_PDF)
