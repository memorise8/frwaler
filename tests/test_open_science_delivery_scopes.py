"""Native two-community source parsing, persistence, dates and failures."""
import csv,json,os,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from tests.test_coupled_site_counts import CoupledContract
from tests.test_final_indirect_counts import DATES

class OpenScienceDeliveryScopes(CoupledContract,unittest.TestCase):
    SITE='open-science-canada-ca-communities';LIST_METHOD='_fetch_list';LIST_MODULE=False
    ORIGINAL='307d8eca-9337-48d3-a296-6ea96f5a75c5';ADDITIONAL='00a6222f-4761-4167-81e1-44007f19557f'
    def item(self,n):
        body='' if n in getattr(self,'excluded',set()) else 'Original scientific publication evidence. '*10
        metadata={'dc.title':[{'value':f'Research {n}','language':'en'}],'dc.description.abstract':[{'value':body,'language':'en'}],'dc.date.issued':[{'value':getattr(self,'dates',['2025-07-11']*8)[n-1]}],'dc.date.accessioned':[{'value':'2026-08-04T13:20:17Z'}]}
        if getattr(self,'no_issued_date',False):metadata.pop('dc.date.issued')
        uid=f'{n:08d}-0000-0000-0000-000000000000';base='https://open-science.canada.ca/server/api/core/items/'+uid
        return {'uuid':uid,'id':uid,'type':'item','name':f'Research {n}','inArchive':True,'discoverable':True,'withdrawn':False,'handle':f'123456789/{n}','metadata':metadata,'_links':{'self':{'href':base},'bundles':{'href':base+'/bundles'}}}
    def crawler(self):
        c=self.configured(self.module.OpenScienceCanadaCaCommunitiesCrawler(self.conn));c.PAGE_SIZE=3
        def fetch(url,context='request'):
            if '/discover/search/objects' in url:
                q=parse_qs(urlsplit(url).query);scope=q['scope'][0];page=int(q['page'][0]);ids=[1,2,3] if scope==self.ORIGINAL else [1,2,3,4,5,6,7,8]
                if getattr(self,'empty_first',False) and scope==self.ORIGINAL:ids=[]
                if getattr(self,'failed_scope',None)==scope:return None
                objects=[{'_embedded':{'indexableObject':self.item(n)}} for n in ids[page*3:(page+1)*3]]
                return {'_embedded':{'searchResult':{'_embedded':{'objects':objects},'page':{'number':page,'size':3,'totalPages':(len(ids)+2)//3,'totalElements':len(ids)}}}}
            uid=url.split('/core/')[1].split('/')[1];n=int(uid[:8])
            if urlsplit(url).path.endswith('/bundles'):
                self.assertEqual(parse_qs(urlsplit(url).query),{'embed':['bitstreams']})
                if getattr(self,'fail_bundles',False):return None
                bit={'uuid':uid,'id':uid,'type':'bitstream','name':f'report-{n}.pdf','bundleName':'ORIGINAL','sizeBytes':123,'checkSum':{'checkSumAlgorithm':'MD5','value':'a'*32},'_links':{'content':{'href':f'https://open-science.canada.ca/server/api/core/bitstreams/{uid}/content'}}}
                biturl=f'https://open-science.canada.ca/server/api/core/bundles/{uid}/bitstreams'
                tree={'_embedded':{'bitstreams':[bit]},'page':{'number':0,'size':20,'totalPages':1,'totalElements':1},'_links':{'self':{'href':biturl}}}
                return {'_embedded':{'bundles':[{'uuid':uid,'name':'ORIGINAL','type':'bundle','_links':{'bitstreams':{'href':biturl}},'_embedded':{'bitstreams':tree}}]},'page':{'number':0,'size':20,'totalPages':1,'totalElements':1},'_links':{'self':{'href':url}}}
            return self.item(n)
        c._curl_json=self.fetch=Mock(side_effect=fetch);c._curl_get=Mock(return_value='<main></main>');return c
    def test_both_delivery_communities_and_overlap_page_reach_all8(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertFalse(c.delivery_source_failure)
        scopes={parse_qs(urlsplit(call.args[0]).query)['scope'][0] for call in self.fetch.call_args_list if '/discover/search/objects' in call.args[0]}
        self.assertEqual(scopes,{self.ORIGINAL,self.ADDITIONAL})
        before=self.identities();self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.identities(),before)
    def test_source_configuration_matches_both_delivery_url_scopes(self):
        source=Path(__file__).resolve().parents[1]/'docs/deliverables/test_URL_수집대조_20260802.csv'
        with source.open(encoding='utf-8-sig') as f:expected={parse_qs(urlsplit(r['URL주소']).query)['scope'][0] for r in csv.DictReader(f) if r['매칭크롤러(site_id)']==self.SITE}
        self.assertEqual(set(self.module.OpenScienceCanadaCaCommunitiesCrawler.COMMUNITY_IDS),expected)
    def test_empty_first_scope_does_not_hide_second(self):
        self.empty_first=True;c=self.crawler();self.assertEqual(c.crawl(),8);self.assertFalse(c.delivery_source_failure)
    def test_later_scope_failure_is_not_successful_completion(self):
        self.failed_scope=self.ADDITIONAL;c=self.crawler();self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.failed_scope=None;self.assertEqual(self.crawler().crawl(),5)
    def test_excluded_first_scope_does_not_hide_later_records(self):
        self.excluded={1,2,3};self.assertEqual(self.crawler().crawl(),5)
    def test_raw_publication_precision_and_accessioned_date_are_separate(self):
        self.dates=DATES;self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw,published_date_status,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        actual=[tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]
        self.assertEqual(actual,[(raw,status,'2026-08-04T13:20:17Z') for raw,status in zip(DATES,['partial','partial','invalid','normalized','unrecognized','ambiguous','normalized','partial'])])
    def test_accessioned_date_does_not_become_publication_date(self):
        self.no_issued_date=True;self.assertEqual(self.crawler().crawl(),8)
        for r in self.conn.execute('SELECT published_date_status,listed_date FROM documents'):
            self.assertEqual(tuple(r.values()) if isinstance(r,dict) else tuple(r),('missing','2026-08-04'))
    def test_failed_attachment_metadata_is_incomplete_not_no_attachment(self):
        self.fail_bundles=True;c=self.crawler();self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_docx_source_is_retained_as_attachment_not_saved_as_pdf(self):
        c=self.crawler();original=self.fetch.side_effect;papers=[];save=c._save_paper
        def fetch(url,context='request'):
            result=original(url,context)
            if '/bundles?' in url:
                result['_embedded']['bundles'][0]['_embedded']['bitstreams']['_embedded']['bitstreams'][0]['name']='ScientificReport.DOCX'
            return result
        def capture(paper):papers.append(dict(paper));return save(paper)
        self.fetch.side_effect=fetch;c._save_paper=capture
        self.assertEqual(c.crawl(),8)
        self.assertTrue(all(not p['pdf_url'] for p in papers))
        for paper in papers:
            attachments=json.loads(paper['metadata'])['bitstreams']
            self.assertEqual(len(attachments),1);self.assertEqual(attachments[0]['name'],'ScientificReport.DOCX');self.assertTrue(attachments[0]['content_url'])
        for row in self.conn.execute('SELECT pdf_url,pdf_downloaded FROM documents'):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            self.assertFalse(values[0]);self.assertFalse(values[1])
        self.assertEqual(c.crawl(),0)

    def test_pdf_selection_skips_missing_content_link_without_losing_other_files(self):
        c=self.crawler()
        refs=[{'name':'missing.pdf','bundleName':'ORIGINAL'}, {'name':'notes.docx','bundleName':'ORIGINAL','_links':{'content':{'href':'https://fixture.invalid/notes.docx'}}}, {'name':'REPORT.PDF','bundleName':'ORIGINAL','_links':{'content':{'href':'https://fixture.invalid/report.pdf'}}}]
        url,summary=c._select_file(refs);self.assertEqual(url,'https://fixture.invalid/report.pdf');self.assertEqual(len(summary),3)

    def test_complete_inline_metadata_does_not_request_redundant_item_details(self):
        c=self.crawler();self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)
        self.assertFalse(any('/core/items/' in call.args[0] and '/bundles?' not in call.args[0] for call in self.fetch.call_args_list))
        self.assertEqual(sum('/bundles?' in call.args[0] for call in self.fetch.call_args_list),8)

    def test_incomplete_inline_metadata_fetches_real_item_representation(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,context='request'):
            result=original(url,context)
            if '/discover/search/objects' in url:
                for r in result['_embedded']['searchResult']['_embedded']['objects']:r['_embedded']['indexableObject'].pop('metadata')
            return result
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),8);self.assertTrue(c.delivery_exhausted)
        self.assertEqual(sum('/core/items/' in x.args[0] and '/bundles?' not in x.args[0] for x in self.fetch.call_args_list),8)

    def test_unresolved_body_gap_marks_job_incomplete_after_later_records(self):
        self.excluded={1};c=self.crawler();self.assertEqual(c.crawl(),7)
        self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

    def test_incomplete_embedded_bundle_counts_and_unsafe_content_are_rejected(self):
        for problem in ('wrong_count','missing_page','missing_embedding','duplicate_bitstream','foreign_content','missing_checksum'):
            with self.subTest(problem=problem):
                c=self.crawler();original=self.fetch.side_effect
                def fetch(url,context='request'):
                    result=original(url,context)
                    if '/bundles?' in url:
                        bundle=result['_embedded']['bundles'][0];tree=bundle['_embedded']['bitstreams'];bit=tree['_embedded']['bitstreams'][0]
                        if problem=='wrong_count':tree['page']['totalElements']=2
                        if problem=='missing_page':tree.pop('page')
                        if problem=='missing_embedding':bundle.pop('_embedded')
                        if problem=='duplicate_bitstream':tree['_embedded']['bitstreams'].append(dict(bit));tree['page']['totalElements']=2
                        if problem=='foreign_content':bit['_links']['content']['href']='https://other.invalid/file.pdf'
                        if problem=='missing_checksum':bit.pop('checkSum')
                    return result
                self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

    def test_zero_limit_makes_no_request(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()
    def test_error_json_is_not_an_empty_scope(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:{'error':'unavailable'}
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_changed_total_is_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,context='request'):
            result=original(url,context)
            if '/discover/search/objects' in url and parse_qs(urlsplit(url).query)['page']==['1']:result['_embedded']['searchResult']['page']['totalElements']+=1
            return result
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_each_saved_paper_attributes_its_actual_community(self):
        c=self.crawler();original=c._save_paper;papers={}
        def save(p):papers[p['external_id']]=dict(p);return original(p)
        c._save_paper=save;self.assertEqual(c.crawl(),8)
        for uid,p in papers.items():
            expected=self.ORIGINAL if int(uid[:8])<=3 else self.ADDITIONAL
            metadata=json.loads(p['metadata']);self.assertEqual(metadata['community_id'],expected);self.assertTrue(metadata['start_url'].endswith('/'+expected))
    def test_incomplete_attachment_pagination_does_not_look_like_full_metadata(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,context='request'):
            result=original(url,context)
            if '/bundles?' in url:result['_embedded']['bundles'][0]['_embedded']['bitstreams']['page'].update(totalPages=2,totalElements=21)
            return result
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_real_transport_access_denial_is_held_without_retry(self):
        c=self.configured(self.module.OpenScienceCanadaCaCommunitiesCrawler(self.conn))
        result=Mock(returncode=22,stdout=b'\n403',stderr=b'HTTP403')
        with patch.object(self.module.subprocess,'run',return_value=result) as run:
            self.assertEqual(c.crawl(),0);self.assertEqual(run.call_count,1)
            self.assertEqual(c.delivery_source_failure,'authentication_failed')
            command=run.call_args.args[0];self.assertIn('-sSL',command);self.assertNotIn('-skL',command);self.assertNotIn('-k',command)
    def test_real_transport_strips_only_appended_status(self):
        c=self.configured(self.module.OpenScienceCanadaCaCommunitiesCrawler(self.conn));body=b'{"note":"HTTP200 in source"}\n'
        with patch.object(self.module.subprocess,'run',return_value=Mock(returncode=0,stdout=body+b'\n200',stderr=b'')):
            self.assertEqual(c._curl_get('https://fixture.invalid/'),body.decode())
    def test_collection_entities_are_counted_for_paging_but_not_fetched_as_documents(self):
        c=self.crawler();original=self.fetch.side_effect;collection='00000009-0000-0000-0000-000000000000'
        def fetch(url,context='request'):
            result=original(url,context)
            if '/discover/search/objects' in url and parse_qs(urlsplit(url).query)['scope']==[self.ADDITIONAL]:
                search=result['_embedded']['searchResult'];search['page']['totalElements']=9
                if search['page']['number']==2:search['_embedded']['objects'].append({'_embedded':{'indexableObject':{'uuid':collection,'type':'collection','name':'Soils','metadata':{'dc.title':[{'value':'Soils'}]}}}})
            return result
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),8);self.assertFalse(c.delivery_source_failure)
        self.assertFalse(any('/items/'+collection in call.args[0] for call in self.fetch.call_args_list))

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class OpenScienceDeliveryScopesPostgres(OpenScienceDeliveryScopes):
    BACKEND='postgres'

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class OpenScienceDeliveryWorker(unittest.TestCase):
    def test_native_worker_counts_scope_overlap_limits_and_cancel(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=OpenScienceDeliveryScopesPostgres(methodName='test_repeat_three_reaches_remainder_and_then_zero')
        try:
            case.setUp();init_delivery_schema(case.conn)
            for limit,cancel,expected in [(3,None,3),(3,None,3),(None,1,1),(None,None,1),(None,None,0)]:
                c=case.crawler();before=case.count();jid=jobs.enqueue_job(case.conn,case.SITE,mode='full',limit_n=limit)
                def should_cancel():
                    if cancel and case.count()>=before+cancel:jobs.cancel_job(case.conn,jid);return True
                    return False
                returned=worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0,should_cancel=should_cancel)
                detail=jobs.job_detail(case.conn,jid)['job']
                self.assertEqual((returned,detail['saved_count'],detail['status']),(expected,expected,'cancelled' if cancel else 'done'))
                self.assertEqual(case.count()-before,expected)
            self.assertEqual(case.count(),8)
        finally:case.doCleanups()

    def test_native_worker_preserves_body_gap_as_failed_after_other_records_saved(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        case=OpenScienceDeliveryScopesPostgres(methodName='test_repeat_three_reaches_remainder_and_then_zero')
        try:
            case.setUp();init_delivery_schema(case.conn);case.excluded={1};c=case.crawler()
            jid=jobs.enqueue_job(case.conn,case.SITE,mode='backfill')
            returned=worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0)
            job=jobs.job_detail(case.conn,jid)['job']
            self.assertEqual((returned,job['saved_count'],job['status']),(7,7,'failed'))
            self.assertIn('item_failed',job['error']);self.assertIsNone(jobs.claim_next_job(case.conn));self.assertFalse(c.delivery_exhausted)
            case.excluded=set();c=case.crawler();self.assertEqual(c.crawl(),1);self.assertEqual(case.count(),8);self.assertTrue(c.delivery_exhausted)
        finally:case.doCleanups()
