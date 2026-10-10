"""Four publisher menu shapes, dates, pagination and actual database lifecycle."""
import csv,json,os,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract

BODY='The original publication describes education and employment research. '*4

def listing(menu,ids,page=1,last=1,date='2026.07.31'):
    headers=['번호','분류','제목','등록일','첨부'] if menu=='23' else ['번호','제목','저자','등록일' if menu=='19' else '발행일','첨부']
    rows=[]
    for uid in ids:
        link=f'''<a onclick="site.{'selectView' if menu in ('19','23') else 'selectDetail'}('{uid}')">Research {uid}</a>'''
        attachment=f'''<button class="icon_file" onclick="site.fileDown('da','{uid}','1','report.pdf')" title="report.pdf">download</button>'''
        cells=[uid,'보도자료',link,date,attachment] if menu=='23' else [uid,link,'Researcher',date,attachment]
        rows.append('<tr>'+''.join('<td>'+x+'</td>' for x in cells)+'</tr>')
    if not ids:rows=['<tr><td colspan="5">검색된 결과가 없습니다.</td></tr>']
    return f'''<form name="searchForm"><input name="menuSn" value="{menu}"><input name="fileType" value="all"></form><table><thead><tr>{''.join('<th>'+x+'</th>' for x in headers)}</tr></thead><tbody>{''.join(rows)}</tbody></table><a class="page_last" onclick="site.selectList({last})"></a><a class="on" title="선택됨">{page}</a>'''

def detail(menu,uid,date='2026.07.31',body=BODY):
    identity=f'<form name="moveForm"><input name="menuSn" value="{menu}"><input name="pstNo" value="{uid}"></form>'
    if menu=='23':
        return identity+f'''<table><tbody><tr><th class="tb_tit" colspan="2">Research {uid}</th></tr><tr><th>등록일</th><td>{date}</td></tr><tr><th>첨부파일</th><td><button class="icon_file_hwp" onclick="site.fileDown('KBD002','{uid}','1')">notice.hwp</button><button class="icon_file_pdf" onclick="site.fileDown('KBD002','{uid}','2')">report.pdf</button></td></tr><tr><td colspan="2"><p>{body}</p></td></tr></tbody></table>'''
    return identity+f'''<h4 class="pub_tit">Research {uid}</h4><dl class="sub_dl"><dt>저자명</dt><dd><span>Researcher<button>Popup</button></span></dd><dt>발행일</dt><dd>{date}</dd><dt>등록일</dt><dd>2026.08.13</dd><dt>DOI</dt><dd>10.1234/example</dd></dl><div id="tl2"><pre>{body}</pre></div><button class="btn_down" onclick="site.fileDown('{uid}','1','report.pdf')" title="report.pdf">전문보기</button>'''

class KrivetScopes(SiteContract,unittest.TestCase):
    SITE='krivet-re-kr-kor'
    def crawler(self):
        c=self.configured(self.module.KrivetReKrKorCrawler(self.conn));self.addCleanup(c._session.close)
        scopes={'15':['PB1','PB2'],'19':['PB3','PB4'],'23':['NH5','NH6'],'12':['PB7','PB8']}
        def fetch(url,params=None,**kwargs):
            params=params or {};menu=params['menuSn']
            if 'pstNo' in params:return detail(menu,params['pstNo'],date=getattr(self,'source_date','2026.07.31'))
            return listing(menu,scopes[menu],int(params.get('pageIndex',1)))
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch));return c
    def test_all_original_delivery_menus_and_public_all_filter(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertFalse(c.delivery_source_failure)
        source=Path(__file__).resolve().parents[1]/'docs/deliverables/test_URL_수집대조_20260802.csv'
        with source.open(encoding='utf-8-sig') as f:expected={parse_qs(urlsplit(r['URL주소']).query)['menuSn'][0] for r in csv.DictReader(f) if r['매칭크롤러(site_id)']==self.SITE}
        calls=[x.args[1] for x in self.fetch.call_args_list if 'pageIndex' in x.args[1]]
        self.assertEqual({x['menuSn'] for x in calls},expected);self.assertEqual(next(x for x in calls if x['menuSn']=='12')['fileType'],'all')
    def test_menu23_title_column_registration_date_and_pdf_attachment(self):
        c=self.crawler();c.crawl();r=self.conn.execute("SELECT * FROM documents WHERE post_number='NH5'").fetchone()
        if not isinstance(r,dict):r=dict(zip([x[0] for x in self.conn.execute('SELECT * FROM documents LIMIT 0').description],r))
        self.assertEqual(r['title'],'Research NH5');self.assertFalse(r['published_date_raw']);self.assertEqual(r['listed_date_raw'],'2026.07.31')
        q=parse_qs(urlsplit(r['pdf_url']).query);self.assertEqual(q['pstgClsfCd'],['KBD002']);self.assertEqual(q['pstgAtchNo'],['2'])
        self.assertNotIn('Researcher',r['abstract']);self.assertIn(BODY.strip(),r['abstract'])
    def test_raw_publication_date_keeps_invalid_suffix(self):
        self.source_date='2026.07.31 uncertain';c=self.crawler();c.crawl();r=self.conn.execute("SELECT published_date_raw,published_date_status FROM documents WHERE post_number='PB1'").fetchone()
        self.assertEqual(tuple(r.values()) if isinstance(r,dict) else tuple(r),('2026.07.31 uncertain','unrecognized'))
    def test_cancel_before_network_and_after_final_save(self):
        c=self.crawler();c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.fetch.assert_not_called();c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=8
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),8)
    def test_cancel_then_resume(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=2
        with self.assertRaises(CrawlCancelled):c.crawl()
        before=self.identities();self.assertEqual(self.crawler().crawl(),6);self.assertTrue(all(self.identities()[k]==v for k,v in before.items()))
    def test_zero_limit_no_network(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()
    def test_failed_list_not_empty_success(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_error_html_not_empty_success(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:'<h1>Access unavailable</h1>';self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_detail_failure_is_incomplete_and_other_rows_continue(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda u,p,**k:None if p.get('pstNo')=='PB2' else original(u,p,**k)
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_catalogue_only_detail_is_explicit_body_gap(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda u,p,**k:detail('15','PB2',body='') if p.get('pstNo')=='PB2' else original(u,p,**k)
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_declared201_pages_not_cut_at200(self):
        c=self.crawler()
        def fetch(u,p,**k):
            if 'pstNo' in p:return detail(p['menuSn'],p['pstNo'])
            if p['menuSn']!='15':return listing(p['menuSn'],[])
            page=int(p['pageIndex']);return listing('15',[f'PB{page}'],page,201)
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),201);self.assertFalse(c.delivery_source_failure)
    def test_repeated_page_and_changed_last_page_report_incomplete(self):
        for changed in (False,True):
            with self.subTest(changed=changed):
                c=self.crawler();original=self.fetch.side_effect
                def fetch(u,p,**k):
                    if 'pstNo' in p:return original(u,p,**k)
                    page=int(p['pageIndex']);return listing(p['menuSn'],['PB1'],1 if page==2 and not changed else page,3 if page==2 and changed else 2)
                self.fetch.side_effect=fetch;c.crawl();self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_time_and_unknown_page_budgets_report_supported_failure(self):
        c=self.crawler()
        with patch.object(self.module,'_WALL_CLOCK_LIMIT',-1):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response')
        c=self.crawler();original=self.fetch.side_effect
        def fetch(u,p,**k):
            raw=original(u,p,**k)
            return raw.replace('class="page_last"','class="unavailable_last"') if 'pageIndex' in p else raw
        self.fetch.side_effect=fetch
        with patch.object(self.module,'_MAX_PAGES',1):
            self.assertEqual(c.crawl(),2)
        self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_overlap_only_menu_does_not_hide_later_menu(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda u,p,**k:listing('19',['PB1','PB2']) if p.get('menuSn')=='19' and 'pageIndex' in p else original(u,p,**k)
        self.assertEqual(c.crawl(),6);self.assertFalse(c.delivery_source_failure)

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class KrivetScopesPostgres(KrivetScopes):BACKEND='postgres'

class KrivetTransport(unittest.TestCase):
    def setUp(self):
        import importlib.util
        path=Path(__file__).resolve().parents[1]/'crawler/sites/custom/krivet-re-kr-kor.py';spec=importlib.util.spec_from_file_location('krivet_transport',path);self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
    def test_403_body_never_becomes_parseable_response_or_retries(self):
        with patch.object(self.module.subprocess,'run',return_value=Mock(returncode=0,stdout=b'<h1>Denied</h1>\n403')) as run:
            with self.assertRaises(PermissionError):self.module._curl_get('https://www.krivet.re.kr/kor/sub.do')
        self.assertEqual(run.call_count,1);self.assertNotIn('-sk',run.call_args.args[0])
    def test_verified200_strips_only_appended_status(self):
        with patch.object(self.module.subprocess,'run',return_value=Mock(returncode=0,stdout=b'<p>200</p>\n200')):
            self.assertEqual(self.module._curl_get('https://www.krivet.re.kr/kor/sub.do'),'<p>200</p>')

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class KrivetNativeWorker(unittest.TestCase):
    def test_actual_worker_cancel_repeat_and_failed_later_scope(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=KrivetScopesPostgres(methodName='test_repeat_three_reaches_remainder_and_then_zero')
        try:
            case.setUp();init_delivery_schema(case.conn)
            for limit,cancel,expected in [(3,None,3),(3,None,3),(None,1,1),(None,None,1),(None,None,0)]:
                c=case.crawler();before=case.count();jid=jobs.enqueue_job(case.conn,case.SITE,mode='full',limit_n=limit)
                def should_cancel():
                    if cancel and case.count()>=before+cancel:jobs.cancel_job(case.conn,jid);return True
                    return False
                returned=worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0,should_cancel=should_cancel)
                job=jobs.job_detail(case.conn,jid)['job']
                self.assertEqual((returned,job['saved_count'],job['status']),(expected,expected,'cancelled' if cancel else 'done'))
            c=case.crawler();original=case.fetch.side_effect
            case.fetch.side_effect=lambda u,p,**k:None if p.get('menuSn')=='12' else original(u,p,**k)
            jid=jobs.enqueue_job(case.conn,case.SITE,mode='full')
            returned=worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0)
            job=jobs.job_detail(case.conn,jid)['job'];self.assertEqual(returned,0);self.assertEqual(job['status'],'failed');self.assertIn('fetch_failed',job['error']);self.assertEqual(case.count(),8)
        finally:case.doCleanups()
