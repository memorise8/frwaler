"""KEI stable source identity, native repeats, and historical row preservation."""
import os
import unittest
from urllib.parse import urlencode
from crawler.base_crawler import CrawlCancelled
from tests.test_delivery_site_new_counts import SiteContract


def detail_url(cid,*,page=1,row=1,name='researchreport'):
    mid='a10101010000' if name=='researchreport' else 'a10102010000'
    return 'https://www.kei.re.kr/elibList.es?'+urlencode(dict(mid=mid,elibName=name,class_id='',act='view',c_id=cid,rn=row,nPage=page,keyField='',keyWord=''))


class KeiIdentity(SiteContract,unittest.TestCase):
    SITE='kei-re-kr-eliblistes'
    def crawler(self):
        c=self.configured(self.module.KeiReKrEliblistesCrawler(self.conn));c._RATE_SLEEP=0;self.addCleanup(c._session.close);self.requests=[]
        def listing(page):
            self.requests.append(page)
            ids={1:range(101,105),2:range(105,109)}.get(page,[])
            return [dict(c_id=str(i),post_number=str(109-i),category='연구',date_raw='2026-08-31',title=f'연구 {i}',url=detail_url(str(i),page=page+getattr(self,'position_shift',0),row=index+1),list_url='https://www.kei.re.kr/elibList.es') for index,i in enumerate(ids)]
        c._fetch_list_page=listing;c._fetch_detail=lambda url,referer:dict(abstract='환경 연구의 본문과 분석 방법 및 결과에 관한 충분한 설명입니다. '*10,published_date='2026-08-31',authors=['연구자'],pdf_url='https://library.kei.re.kr/pyxis-api/1/digital-files/fixture')
        # Identity tests retain controlled responses; production all-scope HTML
        # parsing and durable resumes are covered by test_kei_all_scope_traversal.
        from crawler.kei_source import SCOPES
        from crawler.kei_traversal import digest
        def scope_page(scope,page):
            old=c._fetch_list_page(page) if scope=='researchreport' else []
            items=[dict(r,source_id=r['c_id'],source_key=scope+':'+r['c_id'],scope=scope,attachments=[]) for r in old]
            total,last=getattr(c,'_list_bounds',None) or (8,2)
            if scope!='researchreport':total,last=0,1
            return dict(items=items,total=total,last_page=last,page=page,page_hash=digest(items),url='https://www.kei.re.kr'+SCOPES[scope][0]+'?mid='+SCOPES[scope][1])
        def scope_detail(item,listing_url):
            d=c._fetch_detail(item['url'],referer=listing_url)
            return dict(title=d.get('title') or item['title'],abstract=d.get('abstract',''),authors=d.get('authors',[]),published_date_raw=d.get('published_date') or item['date_raw'],listed_date_raw=item['date_raw'],primary_PDF_candidate=dict(url=d['pdf_url'],filename=d.get('original_filename','')) if d.get('pdf_url') else None)
        c._scope_page=scope_page;c._scope_detail=scope_detail
        return c

    def test_same_document_at_changed_page_does_not_create_more_rows(self):
        self.assertEqual(self.crawler().crawl(),8);before=self.identities();self.position_shift=7
        self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.identities(),before);self.assertEqual(self.count(),8)

    def row(self):
        r=self.conn.execute('SELECT * FROM documents ORDER BY seq_id LIMIT 1').fetchone()
        if hasattr(r,'keys'):return dict(r)
        names=[c[0] for c in self.conn.execute('SELECT * FROM documents WHERE 1=0').description];return dict(zip(names,r))

    def test_historical_id_url_dates_file_flags_and_summary_are_preserved(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=1),1);mark='%s' if self.BACKEND=='postgres' else '?'
        self.conn.execute(f'UPDATE documents SET post_number={mark},summary={mark},pdf_downloaded=1,text_extracted=1,pdf_sha256={mark}',('legacy-ordinal-8','검증용 기존 요약','a'*64));self.conn.commit();before=self.row();self.position_shift=9
        # A known source key can keep the old primary ID without rewriting it.
        self.assertEqual(self.crawler().crawl(limit=1),1);self.assertEqual(self.row(),before);self.assertEqual(self.count(),2)

    def test_query_order_encoded_id_and_navigation_fields_do_not_change_identity(self):
        c=self.crawler();c.crawl(limit=1);backend=__import__('crawler.db_backend',fromlist=['get_backend']).get_backend();seq=self.row()['seq_id']
        url='https://www.kei.re.kr/elibList.es?c_id=%31%30%31&act=view&elibName=researchreport&mid=a10101010000&nPage=77&rn=4&keyWord=water'
        self.assertEqual(backend.find_by_dedup_key(self.conn,self.SITE,'101',url),seq)

    def test_historical_encoded_parameter_and_original_id_are_found(self):
        c=self.crawler();c.crawl(limit=1);mark='%s' if self.BACKEND=='postgres' else '?'
        url=detail_url('101').replace('c_id=101','c%5Fid=%31%30%31')
        self.conn.execute(f'UPDATE documents SET post_number={mark},meta_url={mark}',('original-row-id',url));self.conn.commit();before=self.row();self.position_shift=5
        self.assertEqual(self.crawler().crawl(limit=1),1);self.assertEqual(self.row(),before)

    def test_duplicate_source_id_rows_are_held_not_merged(self):
        c=self.crawler();c.crawl(limit=1);original=self.row();mark='%s' if self.BACKEND=='postgres' else '?'
        self.conn.execute(f'INSERT INTO documents (site_id,post_number,meta_url,title) VALUES ({mark},{mark},{mark},{mark})',(self.SITE,'101',detail_url('101',page=8),original['title']));self.conn.commit();before=self.conn.execute('SELECT count(*) FROM documents').fetchone();self.position_shift=9
        c=self.crawler();c.delivery_mode='backfill';self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertEqual(self.count(),2)
        self.assertEqual(self.row(),original)

    def test_reused_post_id_for_different_scope_requires_review(self):
        c=self.crawler();c.crawl(limit=1);backend=__import__('crawler.db_backend',fromlist=['get_backend']).get_backend()
        with self.assertRaisesRegex(ValueError,'requires_review'):backend.find_by_dedup_key(self.conn,self.SITE,'101',detail_url('101',name='environmentalforum'))
        self.assertEqual(self.count(),1)

    def test_invalid_source_identity_is_not_used_for_dedup(self):
        from crawler.document_identity import kei_elib_id
        for url in ('https://example.com/elibList.es?c_id=101',detail_url('101')+'&c_id=999',detail_url('101').replace('act=view','act=list'),detail_url('101').replace('elibName=researchreport','elibName=unknown'),detail_url('101').replace('mid=a10101010000','mid=a10102010000')):
            with self.assertRaises(ValueError):kei_elib_id(url)

    def test_initial_cancel_and_zero_limit_do_not_read_lists(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.assertEqual(self.requests,[]);c.delivery_should_cancel=lambda:True
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.requests,[])

    def test_other_sites_keep_their_existing_dedup_contract(self):
        from crawler.db_backend import get_backend
        backend=get_backend();other='fixture-other-kei';backend.upsert_site(self.conn,other,other,'https://fixture.invalid')
        doc=dict(site_id=other,post_number='101',meta_url=detail_url('101'),title='Original')
        first=backend.insert_document(self.conn,doc);second=backend.insert_document(self.conn,dict(doc,meta_url=detail_url('101',page=9)))
        self.assertNotEqual(first,second)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class KeiIdentityPostgres(KeiIdentity):
    BACKEND='postgres'

    def test_actual_translation_and_file_queue_references_survive_alias_repeat(self):
        from delivery.db.schema import init_catalogue_schema,init_delivery_schema
        from delivery.worker import jobs
        from delivery.files import jobs as file_jobs
        init_catalogue_schema(self.conn);init_delivery_schema(self.conn)
        self.assertEqual(self.crawler().crawl(),8);seq=self.row()['seq_id']
        self.conn.execute('UPDATE documents SET post_number=%s WHERE seq_id=%s',('legacy-kept-id',seq))
        self.conn.execute("INSERT INTO document_translations(seq_id,source_field,target_locale,source_fingerprint,model_version,prompt_version,state,translation_text) VALUES (%s,'title','en-US',%s,'fixture-model','fixture-prompt','completed','Retained translation')",(seq,'a'*64));self.conn.commit()
        job_id=jobs.enqueue_job(self.conn,self.SITE,mode='backfill',process_files=True)
        self.assertEqual(jobs.claim_next_job(self.conn)['id'],job_id);file_jobs.enqueue(self.conn,job_id,seq)
        before_doc=self.row();before_translation=dict(self.conn.execute('SELECT * FROM document_translations').fetchone());before_file=dict(self.conn.execute('SELECT * FROM document_file_jobs').fetchone())
        self.position_shift=19;self.assertEqual(self.crawler().crawl(),0)
        self.assertEqual(self.row(),before_doc);self.assertEqual(dict(self.conn.execute('SELECT * FROM document_translations').fetchone()),before_translation);self.assertEqual(dict(self.conn.execute('SELECT * FROM document_file_jobs').fetchone()),before_file)

    def test_actual_worker_marks_ambiguous_identity_failed_without_merging(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        c=self.crawler();c.crawl(limit=1);self.conn.execute('INSERT INTO documents(site_id,post_number,meta_url,title) VALUES (%s,%s,%s,%s)',(self.SITE,'101',detail_url('101',page=8),'Historical duplicate'));self.conn.commit();init_delivery_schema(self.conn)
        case=self
        class Factory:
            def __new__(cls,**kwargs):return case.crawler()
        jobs.enqueue_job(self.conn,self.SITE,mode='backfill');job=jobs.claim_next_job(self.conn)
        self.assertEqual(worker.run_job(self.conn,job,{self.SITE:Factory},delay=0),0)
        result=jobs.job_detail(self.conn,job['id'])['job'];self.assertEqual(result['status'],'failed');self.assertEqual(result['error'],'source failure: item_failed');self.assertEqual(self.count(),2)
