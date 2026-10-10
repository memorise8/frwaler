"""Native storage/cursor tests using observed KISDI listing/detail structures."""
import os
import json
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs,urlsplit
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract

BODY='정책 연구의 결과와 근거를 설명하는 실제 본문이며 데이터와 분석 방법 및 결론을 포함합니다. '*5
SCOPES={'basic':('m2101113024153','3934560'),'premium':('m2101113025536','3934550'),'journal':('m2309145696871','5850223')}
IDS={'basic':['101','102','103','104'],'premium':['205','206'],'journal':['307','308']}


def listing(scope,page,*,ids=None,total=None,size=2):
    key,master=SCOPES[scope];all_ids=ids if ids is not None else IDS[scope];total=len(all_ids) if total is None else total;current=all_ids[(page-1)*size:page*size]
    cards=[]
    for aid in current:
        cards.append(f'''<li><a onclick="goView('{master}', '{aid}');"><strong>연구 {aid}</strong><ul><li><strong>발행일</strong>2026-08-31</li></ul></a><a class="download" href="/report/fileDown.do?key={key}&arrMasterId={master}&id={aid}">PDF 다운로드</a></li>''')
    return f'''<form id="frm"><input name="arrMasterId" value="{master}"><input name="recordCountPerPage" value="{size}"></form><strong id="pageCurrEIndex">{total}</strong><strong id="pageCurrSIndex">{page}</strong><strong id="pageMaxIndex">{(total+size-1)//size}</strong><ul id="dataList">{''.join(cards)}</ul>'''


def detail(aid,*,date='2026-08-31',body=BODY,toc='서론 연구방법 결론 '*30):
    return f'<h4 id="h4Title">연구 {aid}</h4><ul id="data"><li><strong>저자</strong>김연구, 이정책</li><li><strong>발행일</strong>{date}</li></ul><div id="tab1">요약 {body}</div><div id="tab2">목차 {toc}</div>'


class KisdiScopes(SiteContract,unittest.TestCase):
    SITE='kisdi-re-kr-report'
    def crawler(self):
        c=self.configured(self.module.KisdiReKrReportCrawler(self.conn));self.addCleanup(c._session.close)
        def curl(method,url,post_fields=None):
            q=parse_qs(urlsplit(url).query);master=q['arrMasterId'][0];scope=next(s for s,(k,m) in SCOPES.items() if m==master)
            self.assertEqual(q['key'],[SCOPES[scope][0]])
            if urlsplit(url).path=='/report/list.do':
                self.assertEqual(method,'GET');return listing(scope,int(q['pageIndex'][0]))
            self.assertEqual(method,'POST');self.assertEqual(post_fields,{'pageIndex':'1'});self.assertEqual(q['masterId'],[master])
            return detail(q['artId'][0],date=getattr(self,'source_date','2026-08-31'))
        self.fetch=self.enterContext(patch.object(c,'_curl',side_effect=curl));return c

    def test_all_three_master_scopes_preserve_published_download_links(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)
        rows=self.conn.execute('SELECT post_number,meta_url,pdf_url FROM documents').fetchall()
        for raw in rows:
            aid,url,pdf=(raw['post_number'],raw['meta_url'],raw['pdf_url']) if isinstance(raw,dict) else raw
            scope=next(s for s,ids in IDS.items() if aid in ids);key,master=SCOPES[scope]
            self.assertEqual(parse_qs(urlsplit(url).query),dict(key=[key],masterId=[master],arrMasterId=[master],artId=[aid]))
            self.assertEqual(parse_qs(urlsplit(pdf).query),dict(key=[key],arrMasterId=[master],id=[aid]))

    def test_raw_partial_and_invalid_dates_are_not_invented(self):
        for raw,precision,status in [('2026-08','month','valid'),('2026-08-31 확인중','unknown','unrecognized')]:
            self.source_date=raw;c=self.crawler();self.assertEqual(c.crawl(limit=1),1)
            row=self.conn.execute('SELECT published_date_raw,published_date_precision,published_date_status,listed_date_raw FROM documents').fetchone()
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            self.assertEqual(values[0],raw);self.assertEqual(values[3],'2026-08-31')
            if status=='unrecognized':self.assertEqual(values[2],status)
            else:self.assertEqual(values[1],precision)
            self.conn.execute('DELETE FROM documents');self.conn.commit()

    def test_zero_and_cancel_before_network(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called();c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.fetch.assert_not_called()

    def test_contents_alone_does_not_count_as_body(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda method,url,post_fields=None:detail('104',body='') if 'artId=104' in url else original(method,url,post_fields)
        self.assertEqual(c.crawl(),7);self.assertFalse(c.delivery_exhausted);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertEqual(self.crawler().crawl(),1)

    def test_no_invented_download_when_source_has_no_link(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(method,url,post_fields=None):
            raw=original(method,url,post_fields)
            if 'list.do' in url:
                soup=self.module._make_soup(raw)
                for a in soup.select('a.download'):a.decompose()
                return str(soup)
            return raw
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(limit=1),1)
        row=self.conn.execute('SELECT pdf_url FROM documents').fetchone();self.assertIsNone(row['pdf_url'] if isinstance(row,dict) else row[0])

    def test_wrong_master_or_file_target_is_rejected(self):
        for changed in ('master','file'):
            c=self.crawler();original=self.fetch.side_effect
            def fetch(method,url,post_fields=None):
                raw=original(method,url,post_fields)
                return raw.replace("goView('3934560'","goView('3934550'") if changed=='master' else raw.replace('arrMasterId=3934560&id=','arrMasterId=3934550&id=')
            self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)

    def test_cross_master_same_art_id_is_held_without_merge(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda method,url,post_fields=None:listing('premium',1,ids=['101','206']) if '/list.do?' in url and '3934550' in url else original(method,url,post_fields)
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)
        row=self.conn.execute("SELECT meta_url FROM documents WHERE post_number='101'").fetchone();self.assertIn('masterId=3934560',row['meta_url'] if isinstance(row,dict) else row[0])

    def test_existing_conflicting_identity_does_not_create_new_row(self):
        c=self.crawler();c.crawl(limit=1);mark='%s' if self.BACKEND=='postgres' else '?'
        self.conn.execute(f'UPDATE documents SET meta_url={mark}',('https://www.kisdi.re.kr/old-identity',));self.conn.commit()
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertEqual(self.count(),8)
        row=self.conn.execute("SELECT meta_url FROM documents WHERE post_number='101'").fetchone();self.assertEqual(row['meta_url'] if isinstance(row,dict) else row[0],'https://www.kisdi.re.kr/old-identity')

    def test_error_html_and_transport_denials_are_not_empty_completion(self):
        for result,reason in [('<h1>Error</h1>','invalid_response'),(None,'fetch_failed'),(PermissionError('HTTP 403'),'authentication_failed'),(PermissionError('HTTP 429'),'rate_limited'),(ConnectionError('HTTP 503'),'fetch_failed')]:
            c=self.crawler();self.fetch.side_effect=result if isinstance(result,Exception) else lambda *a,**kw:result
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,reason);self.assertFalse(c.delivery_exhausted)

    def test_no_200_page_cap(self):
        c=self.crawler();original=self.fetch.side_effect;ids=[str(1000+n) for n in range(201)]
        def fetch(method,url,post_fields=None):
            if '/list.do?' in url and '3934560' in url:return listing('basic',int(parse_qs(urlsplit(url).query)['pageIndex'][0]),ids=ids,size=1)
            return original(method,url,post_fields)
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),205);self.assertTrue(c.delivery_exhausted)
        self.assertIn(self.module._list_url('basic',201),[x.args[1] for x in self.fetch.call_args_list])


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class KisdiScopesPostgres(KisdiScopes):BACKEND='postgres'


class KisdiCursor(KisdiScopes):
    def backfill(self,cursor=None):
        c=self.crawler();c.delivery_mode='backfill';c.delivery_cursor=cursor;return c

    def test_resume_three_three_two_across_all_scopes(self):
        cursor=None
        for expected in (3,3,2):
            c=self.backfill(cursor);self.assertEqual(c.crawl(limit=3),expected);cursor=json.loads(json.dumps(c.delivery_pending_cursor));self.assertTrue(all(not isinstance(v,(list,dict)) for v in cursor.values()))
        self.assertEqual(self.count(),8);self.assertTrue(c.delivery_exhausted)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_exhausted)

    def test_cancel_after_third_commit_preserves_exact_position(self):
        c=self.backfill();c.delivery_should_cancel=lambda:self.count()>=3
        with self.assertRaises(CrawlCancelled):c.crawl()
        cursor=c.delivery_pending_cursor;self.assertEqual((cursor['scope'],cursor['page'],cursor['offset']),('basic',2,1))
        before=self.identities();c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertTrue(all(self.identities()[key]==value for key,value in before.items()))

    def test_body_failure_holds_last_completed_item(self):
        c=self.backfill();original=self.fetch.side_effect
        self.fetch.side_effect=lambda method,url,post_fields=None:detail('104',body='') if 'artId=104' in url else original(method,url,post_fields)
        self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_source_failure,'item_failed');cursor=c.delivery_pending_cursor;self.assertEqual(cursor['offset'],1)
        c=self.backfill(cursor);self.assertEqual(c.crawl(),5);self.assertTrue(c.delivery_exhausted)

    def test_previous_current_total_size_or_pdf_changes_reject_cursor(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for kind in ('previous','current','total','size','pdf'):
            c=self.backfill(cursor);original=self.fetch.side_effect
            def fetch(method,url,post_fields=None):
                raw=original(method,url,post_fields)
                if url==self.module._list_url('basic',1) and kind=='previous':return raw.replace('연구 102','바뀐 제목')
                if url==self.module._list_url('basic',2):
                    if kind=='current':return raw.replace('연구 104','바뀐 제목')
                    if kind=='total':return raw.replace('id="pageCurrEIndex">4','id="pageCurrEIndex">5')
                    if kind=='size':return raw.replace('name="recordCountPerPage" value="2"','name="recordCountPerPage" value="10"')
                    if kind=='pdf':return raw.replace('class="download"','class="not-download"')
                return raw
            self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertIsNone(c.delivery_pending_cursor)

    def test_malformed_cursor_fails_without_network(self):
        c=self.backfill();c.crawl(limit=3);cursor=c.delivery_pending_cursor
        for fields in ({'scope':'all'},{'version':2},{'page':False},{'offset':3},{'page_hash':''},{'page_size':0}):
            c=self.backfill(dict(cursor,**fields));self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'configuration');self.fetch.assert_not_called()

    def test_wall_budget_and_full_rescan_mode(self):
        c=self.backfill()
        with patch.object(self.module,'_WALL_SECONDS',.5),patch.object(self.module.time,'monotonic',side_effect=lambda:1 if self.count()>=3 else 0):self.assertEqual(c.crawl(),3)
        self.assertIsNone(c.delivery_source_failure);cursor=c.delivery_pending_cursor
        c=self.crawler();c.delivery_cursor=cursor;self.assertEqual(c.crawl(),5);self.assertIsNone(c.delivery_pending_cursor)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class KisdiCursorPostgres(KisdiCursor):BACKEND='postgres'


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class KisdiCursorWorker(unittest.TestCase):
    def test_native_worker_cancel_resume_auto_chain(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=KisdiCursorPostgres(methodName='test_resume_three_three_two_across_all_scopes')
        try:
            case.setUp();init_delivery_schema(case.conn)
            class Factory:
                DELIVERY_CURSOR_VERSION=case.module.KisdiReKrReportCrawler.DELIVERY_CURSOR_VERSION
                DELIVERY_ORDER='arbitrary'
                def __new__(cls,**kw):return case.crawler()
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3)
            self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:Factory},delay=0),3)
            job=jobs.claim_next_job(case.conn)
            def cancel():
                if case.count()>=4:jobs.cancel_job(case.conn,job['id']);return True
                return False
            self.assertEqual(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0,should_cancel=cancel),1)
            self.assertEqual(jobs.job_detail(case.conn,job['id'])['job']['status'],'cancelled');self.assertIsNone(jobs.claim_next_job(case.conn))
            jobs.enqueue_job(case.conn,case.SITE,mode='backfill',limit_n=3);counts=[]
            for _ in range(8):
                job=jobs.claim_next_job(case.conn)
                if not job:break
                counts.append(worker.run_job(case.conn,job,{case.SITE:Factory},delay=0))
            self.assertEqual(counts,[3,1]);self.assertEqual(case.count(),8)
            self.assertIsNotNone(case.conn.execute('SELECT completed_at FROM crawl_site_progress').fetchone()['completed_at'])
        finally:case.doCleanups()


class KisdiTransport(unittest.TestCase):
    def setUp(self):
        import importlib.util
        from pathlib import Path
        path=Path(__file__).resolve().parents[1]/'crawler/sites/custom/kisdi-re-kr-report.py';spec=importlib.util.spec_from_file_location('kisdi_transport',path);self.m=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.m)
        self.c=self.m.KisdiReKrReportCrawler(None);self.addCleanup(self.c._session.close)

    def test_http_status_denial_and_service_unavailable_stop_without_retry(self):
        for status,error in [(403,PermissionError),(429,PermissionError),(503,ConnectionError)]:
            with patch.object(self.m.subprocess,'run',return_value=Mock(returncode=0,stdout=f'error\n{status}'.encode())) as run:
                with self.assertRaises(error):self.c._curl('GET',self.m._LIST_URL)
                self.assertEqual(run.call_count,1);self.assertNotIn('-sk',run.call_args.args[0])

    def test_success_and_cancellation(self):
        with patch.object(self.m.subprocess,'run',return_value=Mock(returncode=0,stdout=b'<h1>Source</h1>\n200')) as run:
            self.assertEqual(self.c._curl('POST',self.m._DETAIL_URL,{'pageIndex':'1'}),'<h1>Source</h1>')
            self.c.delivery_should_cancel=lambda:True
            with self.assertRaises(CrawlCancelled):self.c._curl('GET',self.m._LIST_URL)
            self.assertEqual(run.call_count,1)
