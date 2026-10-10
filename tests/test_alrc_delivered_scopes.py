"""Both delivered ALRC scopes, DB identity and source-anchored restart contracts."""
import html
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from crawler.alrc_scope import SCOPES, list_url
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract, InterruptedAfterCommit

BASE = 'https://www.alrc.gov.au'


def listing(scope, page):
    ids = {('final-report', 1): [1, 2, 3], ('final-report', 2): [4, 5],
           ('delivered-search', 1): [2, 6, 7], ('delivered-search', 2): [8]}[(scope, page)]
    rows = ''.join(f'<div class="section-listing-item"><h2><a href="{BASE}/doc/{n}/">Report {n}</a></h2>'
                   '<span class="section-listing-item-date">29.02.2012</span>'
                   '<span class="section-listing-item-type">News</span>'
                   '<p>Listing descriptions must not replace a missing source article body.</p></div>' for n in ids)
    pager = ''.join(f'<span class="page current">{n}</span>' if n == page else
                    f'<a class="page" href="{html.escape(list_url(scope,n))}">{n}</a>' for n in (1, 2))
    if page == 1:
        pager += f'<a class="next" href="{html.escape(list_url(scope,2))}">Next</a>'
    return f'<html><body>{rows}<div class="tw-pagination">{pager}</div></body></html>'


def detail(n):
    return (f'<html><head><link rel="canonical" href="{BASE}/doc/{n}/">'
            f'<link rel="shortlink" href="{BASE}/?p={n}">'
            '<script type="application/ld+json">{"@graph":[{"@type":"Article",'
            '"datePublished":"2012-02-29T11:30:00+10:00"}]}</script></head>'
            f'<body><main><h1 class="post-title">Report {n}</h1><p>'
            'A source article explaining the law reform findings and the evidence behind them. '
            '</p></main></body></html>')


class AlrcCounts(SiteContract, unittest.TestCase):
    SITE = 'alrc-gov-au-publications'

    def setUp(self):
        super().setUp()
        self.responses = {list_url(s,p): listing(s,p) for s in SCOPES for p in (1,2)}
        self.responses.update({f'{BASE}/doc/{n}/': detail(n) for n in range(1,9)})
        self.calls = []
        self.enterContext(patch.object(self.module, '_curl_get', side_effect=self.fetch))

    def fetch(self, url):
        self.calls.append(url)
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    def crawler(self, cursor=None):
        c = self.configured(self.module.ALRCGovAuPublicationsCrawler(self.conn, delay=0))
        self.addCleanup(c._session.close)
        if cursor is not None:
            c.delivery_mode = 'backfill'
            c.delivery_cursor = cursor
        return c

    def backfill(self, cursor=None):
        c = self.crawler(cursor)
        c.delivery_mode = 'backfill'
        return c

    def test_both_scopes_overlap_and_raw_dates_are_preserved(self):
        c = self.backfill()
        self.assertEqual(c.crawl(),8)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual(c.delivery_cursor_items_done,9)
        self.assertEqual(self.calls.count(BASE+'/doc/2/'),1)
        row = self.conn.execute('SELECT * FROM documents WHERE post_number=\'1\'').fetchone()
        if not isinstance(row,dict):
            row = dict(zip([v[0] for v in self.conn.execute('SELECT * FROM documents').description],row))
        self.assertEqual(row['listed_date'],'2012-02-29')
        self.assertEqual(row['listed_date_raw'],'29.02.2012')
        self.assertEqual(row['published_date_raw'],'2012-02-29T11:30:00+10:00')

    def test_partial_pages_resume_through_both_scopes_and_repeat_zero(self):
        cursor = None
        for expected in (3,3,2,0):
            c = self.backfill(cursor)
            self.assertEqual(c.crawl(limit=3),expected)
            cursor = c.delivery_pending_cursor
        self.assertEqual(self.count(),8)
        self.assertTrue(c.delivery_exhausted)
        self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),('delivered-search',2,1))
        self.assertEqual(self.crawler().crawl(),0)

    def test_cancel_after_save_then_interruption_after_commit_replays_safely(self):
        c = self.backfill()
        c.delivery_should_cancel = lambda:self.count()>=2
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],2)
        c = self.backfill(c.delivery_pending_cursor)
        def stop(seq):raise InterruptedAfterCommit()
        c.delivery_document_saved = stop
        with self.assertRaises(InterruptedAfterCommit):c.crawl()
        self.assertEqual(c.delivery_pending_cursor['offset'],2)
        before = self.identities()
        c = self.backfill(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(),5)
        self.assertTrue(c.delivery_exhausted)
        self.assertTrue(all(self.identities()[k]==v for k,v in before.items()))

    def test_changed_current_and_previous_pages_hold_without_advancing(self):
        c = self.backfill();c.crawl(limit=4);cursor=c.delivery_pending_cursor
        for page in (1,2):
            url=list_url('final-report',page);old=self.responses[url]
            self.responses[url]=old.replace('Report','Changed report')
            c=self.backfill(cursor);self.assertEqual(c.crawl(),0)
            self.assertEqual(c.delivery_source_failure,'invalid_response')
            self.assertIsNone(c.delivery_pending_cursor)
            self.responses[url]=old

    def test_body_gap_canonical_mismatch_and_access_failure_keep_exact_position(self):
        url=BASE+'/doc/2/';original=self.responses[url]
        cases = [(original.replace('A source article explaining the law reform findings and the evidence behind them. ',''),'item_failed'),
                 (original.replace(f'href="{url}"',f'href="{BASE}/wrong/"'),'item_failed'),
                 (PermissionError('HTTP 429'),'rate_limited')]
        for raw,reason in cases:
            self.responses[url]=raw;c=self.backfill();c.crawl()
            self.assertEqual(c.delivery_source_failure,reason)
            self.assertEqual(c.delivery_pending_cursor['offset'],1)
            self.assertFalse(c.delivery_exhausted)
        self.responses[url]=original;c=self.backfill(c.delivery_pending_cursor)
        self.assertEqual(c.crawl(),7);self.assertTrue(c.delivery_exhausted)

    def test_page_scope_boundary_missing_rows_and_false_empty_never_exhaust(self):
        url=list_url('final-report',1);original=self.responses[url]
        cases=[original.replace('class="next"','class="lost"'),
               original.replace(list_url('final-report',2),BASE+'/wrong/'),
               original.replace('<h2><a href=', '<h2><b data-href='),
               '<html><body>Error page</body></html>']
        for raw in cases:
            self.responses[url]=raw;c=self.backfill();self.assertEqual(c.crawl(),0)
            self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_invalid_cursor_cancel_and_zero_limit_make_no_requests(self):
        for cursor in ({},{'version':False},{'version':1,'scope':'unknown'}):
            c=self.backfill(cursor);self.assertEqual(c.crawl(),0)
            self.assertEqual(c.delivery_source_failure,'configuration')
        c=self.backfill();self.assertEqual(c.crawl(limit=0),0)
        c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.calls,[])

    def test_short_body_paragraphs_and_nested_lists_are_kept_once(self):
        raw = '<html><main><h1>Title</h1><p class="post-date">29.02.2012</p>' \
              '<section class="wysiwyg"><h2>Findings</h2><p>Short paragraph.</p>' \
              '<ul><li><p>Nested item.</p></li></ul></section></main>' \
              '<footer><p>Unrelated footer text.</p></footer></html>'
        self.assertEqual(self.module._extract_abstract(self.module._make_soup(raw)),
                         'Findings Short paragraph. Nested item.')

    def test_unrelated_footer_pdf_is_not_promoted_to_document_file(self):
        raw = '<html><main><h1>Title</h1><p>Source body</p></main>' \
              '<footer><a href="/privacy.pdf">Final Report</a></footer></html>'
        self.assertIsNone(self.module._best_pdf_link(self.module._make_soup(raw)))

    def test_budget_never_exhausts_and_progress_is_resumable(self):
        c=self.backfill();c._alrc_wall_seconds=lambda:0
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
        c=self.backfill();c.delivery_document_saved=lambda seq:setattr(c,'_alrc_wall_seconds',lambda:0)
        self.assertEqual(c.crawl(),1);self.assertIsNone(c.delivery_source_failure)
        self.assertFalse(c.delivery_exhausted)
        c=self.backfill(c.delivery_pending_cursor);self.assertEqual(c.crawl(),7)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class AlrcPostgres(AlrcCounts):
    BACKEND='postgres'

    def test_actual_worker_automatic_continuation_cancel_and_terminal(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        init_delivery_schema(self.conn)
        for index,(cancel,want) in enumerate(((None,3),(1,1),(None,3),(None,1))):
            c=self.backfill();before=self.count()
            if index in (0,2):jobs.enqueue_job(self.conn,c.site_id,mode='backfill',limit_n=3)
            job=jobs.claim_next_job(self.conn);self.assertIsNotNone(job)
            def cancelled():
                if cancel and self.count()>=before+cancel:
                    jobs.cancel_job(self.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(self.conn,job,{c.site_id:lambda **kw:c},delay=0,should_cancel=cancelled),want)
            state=jobs.job_detail(self.conn,job['id'])['job']
            self.assertEqual(state['status'],'cancelled' if cancel else 'done')
        p=self.conn.execute('SELECT * FROM crawl_site_progress').fetchone()
        self.assertEqual(p['items_done'],9);self.assertIsNotNone(p['completed_at'])
        self.assertEqual(self.count(),8)
        self.assertEqual(self.conn.execute("SELECT count(*) AS n FROM crawl_job_logs WHERE event='stalled'").fetchone()['n'],0)


class AlrcTransport(unittest.TestCase):
    def test_error_status_partial_body_and_redirect_are_never_articles(self):
        import importlib.util
        from tests.test_delivery_site_new_counts import ROOT
        spec=importlib.util.spec_from_file_location('alrc_transport',ROOT/'crawler/sites/custom/alrc-gov-au-publications.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        for status,rc,error in ((403,0,PermissionError),(429,0,PermissionError),(503,0,PermissionError),(302,0,ValueError),(200,18,ValueError)):
            with patch.object(module.subprocess,'run',return_value=SimpleNamespace(stdout=f'<html>not accepted</html>\n{status}'.encode(),returncode=rc)):
                with self.assertRaises(error):module._curl_get(BASE)
