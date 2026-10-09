"""Annual-report native HTML/XML paths with actual persistence and recovery."""
import json,os,re,unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from crawler.base_crawler import CrawlCancelled
from tests.test_coupled_site_counts import CoupledContract

DATES=['2025','July 2025','2025-02-30','2025-07-11T12:34:56Z','2025-07-11 suffix','03/04/2025','July 11, 2025','2025-07']
class AnnualContract(CoupledContract):
    LIST_METHOD='_curl_get';LIST_MODULE=True
    def test_zero_limit_does_not_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()
    def test_failed_entry_is_not_successful_empty_collection(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_cancel_after_last_save_is_observed(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=8
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),8)
    def test_source_dates_retain_entire_raw_value(self):
        c=self.crawler();self.date_values=DATES;self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw,published_date_status FROM documents ORDER BY seq_id').fetchall()
        actual=[tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]
        self.assertEqual(actual,list(zip(DATES,['partial','partial','invalid','normalized','unrecognized','ambiguous','normalized','partial'])))

class MoeAnnualCounts(AnnualContract,unittest.TestCase):
    SITE='moe-gov-cn-jyb_xxgk'
    def crawler(self):
        c=self.configured(self.module.MoeGovCnJybXxgkCrawler(self.conn))
        def fetch(url,**kw):
            if url==self.module._LIST_URL:
                return '<dl id="list">'+''.join(f'<dd><a href="/annual/t20250711_{n}.html" title="年度报告{n}">年度报告{n}</a><span class="x_gkndbg_pcdate">{getattr(self,"date_values",["2025-07-11"]*8)[n-1]}</span></dd>' for n in range(1,9))+'</dl>'
            n=int(re.search(r'_(\d+)\.html',url)[1]);return '<main><div class="TRS_Editor"><p>'+('教育部政府信息公开年度报告的公开原文内容。'*10)+'</p></div></main>'
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch));return c
    def test_detail_failure_is_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,**kw:original(url,**kw) if url==self.module._LIST_URL else None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_article_text_excludes_footer_and_script_contents(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw)+'<footer>UNRELATED FOOTER '+('机构导航 '*50)+'</footer>'
        self.assertEqual(c.crawl(),8)
        for row in self.conn.execute('SELECT abstract FROM documents'):
            text=row['abstract'] if isinstance(row,dict) else row[0]
            self.assertNotIn('UNRELATED',text);self.assertNotIn('TRS_Editor',text);self.assertFalse(text.startswith('">'))
    def test_relative_pdf_uses_article_directory(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw)+'<a href="./attachment.pdf">附件</a>'
        self.assertEqual(c.crawl(),8)
        for row in self.conn.execute('SELECT pdf_url FROM documents'):
            self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],'http://www.moe.gov.cn/annual/attachment.pdf')

class CnipaAnnualCounts(AnnualContract,unittest.TestCase):
    SITE='english-cnipa-gov-cn-col'
    def crawler(self):
        c=self.configured(self.module.CnipaAnnualReportCrawler(self.conn))
        def fetch(url,**kw):
            if url==self.module._START_URL:return '<div class="column_list"><a href="/col/col1001/index.html">2025</a><a href="/col/col1002/index.html">2024</a></div>'
            if '/col/' in url:return '<script>unitid:42</script>'
            if 'dataproxy.jsp' in url:
                q=parse_qs(urlsplit(url).query);col=q['columnid'][0];page=int(q['page'][0]);ids=range(7,9) if col=='1002' else range(1,6) if page==1 else [6]
                records=''.join(f'<record><![CDATA[<a title="Annual section {n}" href="/attach/0/report-{n}.pdf">Report</a><!-- <a href="/art/2025/7/11/art_1336_{n}.html">Article</a> --><span>({getattr(self,"date_values",["2025-07-11"]*8)[n-1]})</span>]]></record>' for n in ids)
                return '<datastore>'+records+f'<totalpage>{1 if col=="1002" else 2}</totalpage></datastore>'
            if '/art/' in url:return '<meta name="description" content="'+('Publisher supplied description of this report. '*5)+'">'
            raise AssertionError(url)
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch));return c
    def test_failed_detail_is_incomplete_instead_of_synthetic_success(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,**kw:None if '/art/' in url else original(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_missing_page_count_does_not_imply_single_page(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,**kw:re.sub(r'<totalpage>\d+</totalpage>','',original(url,**kw))
        c.crawl();self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_failed_second_proxy_page_keeps_prefix_and_reports_failure(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,**kw:None if 'dataproxy.jsp?page=2' in url else original(url,**kw)
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_repeated_proxy_page_reports_incomplete(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,**kw:original(url.replace('dataproxy.jsp?page=2','dataproxy.jsp?page=1'),**kw)
        self.assertEqual(c.crawl(),7);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_metadata_only_catalog_record_does_not_invent_report_contents(self):
        c=self.crawler();original=self.fetch.side_effect;self.fetch.side_effect=lambda url,**kw:'<html></html>' if '/art/' in url else original(url,**kw)
        papers=[];save=c._save_paper
        def emit(paper):papers.append(dict(paper));return save(paper)
        c._save_paper=emit;self.assertEqual(c.crawl(),8)
        self.assertTrue(all(p['abstract'].startswith('Catalog metadata only') for p in papers))
        self.assertTrue(all(json.loads(p['metadata'])['abstract_source']=='catalog_metadata_only' for p in papers))
    def test_pdf_only_record_retains_source_url_without_invented_article_id(self):
        c=self.crawler();url='https://english.cnipa.gov.cn/attach/0/full-report.pdf'
        self.assertTrue(c._process_item({'title':'Annual report','pdf_url':url,'col_id':'1001','date_str':'2025'},'2025'))
        row=self.conn.execute('SELECT meta_url FROM documents').fetchone();self.assertEqual(row['meta_url'] if isinstance(row,dict) else row[0],url)

    def test_apostrophes_and_entities_in_titles_are_preserved(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw).replace('Annual section',"Commissioner's &quot;Annual&quot; section")
        self.assertEqual(c.crawl(),8)
        rows=self.conn.execute('SELECT title FROM documents ORDER BY seq_id').fetchall()
        self.assertEqual([r['title'] if isinstance(r,dict) else r[0] for r in rows],[f'Commissioner\'s "Annual" section {n}' for n in range(1,9)])

    def test_body_and_detail_pdf_are_saved_without_footer_or_caption_pollution(self):
        c=self.crawler();original=self.fetch.side_effect;body='Actual publisher article text and annual statistics. '*5
        def fetch(url,**kw):
            raw=original(url,**kw)
            if 'dataproxy.jsp' in url:raw=re.sub(r' href="/attach/0/report-\d+\.pdf"','',raw)
            if '/art/' in url:raw='<div class="en_article"><p>'+body+'</p><a href="/2018-06/annual.pdf">Download caption</a></div><footer>Wrong footer</footer>'
            return raw
        self.fetch.side_effect=fetch;papers=[];save=c._save_paper
        def emit(p):papers.append(dict(p));return save(p)
        c._save_paper=emit;self.assertEqual(c.crawl(),8)
        for paper in papers:
            self.assertEqual(paper['abstract'],body.strip())
            self.assertEqual(paper['pdf_url'],'https://english.cnipa.gov.cn/2018-06/annual.pdf')
            self.assertEqual(json.loads(paper['metadata'])['abstract_source'],'article_body')

    def test_pdf_caption_is_not_mistaken_for_body_and_multiple_files_are_explicit(self):
        c=self.crawler();original=self.fetch.side_effect
        def fetch(url,**kw):
            raw=original(url,**kw)
            if 'dataproxy.jsp' in url:raw=re.sub(r' href="/attach/0/report-\d+\.pdf"','',raw)
            if '/art/' in url:raw='<div class="en_article"><a href="/a.pdf">'+('Report title '*10)+'</a><a href="/b.pdf">Annex</a></div>'
            return raw
        self.fetch.side_effect=fetch;papers=[];save=c._save_paper
        def emit(p):papers.append(dict(p));return save(p)
        c._save_paper=emit;self.assertEqual(c.crawl(),8)
        for paper in papers:
            self.assertTrue(paper['abstract'].startswith('Catalog metadata only'))
            self.assertIsNone(paper['pdf_url'])
            self.assertTrue(json.loads(paper['metadata'])['primary_pdf_ambiguous'])

    def test_meta_description_preserves_apostrophes(self):
        text="Commissioner's source description with quoted document information. "*3
        self.assertEqual(self.module._extract_meta_tags('<meta name="description" content="'+text+'">')['description'],text)

    def test_repeated_download_caption_paragraph_is_not_document_body(self):
        caption='Ⅶ Exchanges with Hong Kong SAR, Macao SAR and Taiwan, China'
        body,pdfs=self.module._article_content('<div class="en_article"><p>'+caption+'</p><a href="/2017-10/report.pdf">'+caption+'</a></div>','https://english.cnipa.gov.cn/art/report.html')
        self.assertEqual(body,'')
        self.assertEqual(pdfs,['https://english.cnipa.gov.cn/2017-10/report.pdf'])

    def test_legacy_download_url_entities_are_decoded_before_persistence(self):
        c=self.crawler();html='<a href="https://english.cnipa.gov.cn/module/download/down.jsp?i_ID=186070&amp;colID=3261"><img alt="" src="/cover.png"></a>'
        items=c._parse_page_items(html,'3261','2022')
        self.assertEqual(len(items),1);self.assertTrue(c._process_item(items[0],'2022'))
        row=self.conn.execute('SELECT pdf_url,meta_url FROM documents').fetchone()
        values=list(row.values()) if isinstance(row,dict) else list(row)
        self.assertEqual(values,['https://english.cnipa.gov.cn/module/download/down.jsp?i_ID=186070&colID=3261']*2)

    def test_proxy_safety_cap_retains_prefix_without_unsupported_failure_code(self):
        c=self.crawler()
        def fetch(url,**kw):
            n=int(parse_qs(urlsplit(url).query)['page'][0])
            return f'<datastore><record><![CDATA[<a title="Section {n}" href="/art/2025/7/11/art_1336_{n}.html">Report</a>]]></record><totalpage>201</totalpage></datastore>'
        self.fetch.side_effect=fetch
        items=c._fetch_via_dataproxy('1336','42','2025')
        self.assertEqual(len(items),200);self.assertEqual(c.delivery_source_failure,'invalid_response')

for cls in (MoeAnnualCounts,CnipaAnnualCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg
