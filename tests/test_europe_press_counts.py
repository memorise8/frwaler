"""Three native publisher HTML flows with actual SQLite/PostgreSQL persistence."""
import os,re,unittest
from unittest.mock import Mock,patch
from urllib.parse import parse_qs,urlsplit
from crawler.base_crawler import CrawlCancelled
from tests.test_coupled_site_counts import CoupledContract

class EuropePressContract(CoupledContract):
    def crawler(self):
        c=self.configured(getattr(self.module,self.CLASS)(self.conn))
        if self.SITE=='bmleh-de-siteglobals':c._RESULTS_PER_PAGE=5
        def fetch(url,**kw):
            match=re.search(r'/report-(\d+)(?:\.html)?$',url)
            if match:
                n=int(match[1]);body='' if n in getattr(self,'excluded',set()) else 'Original publisher research and publication evidence. '*10
                date=getattr(self,'date_values',['2025-07-11T12:34:56Z']*8)[n-1]
                return f'<title>Report {n} | The United Nations Office at Geneva</title><div class="c-intro"><h1 class="c-intro__headline">Report {n}</h1></div><p>{body}</p><p class="c-teaser__meta"><time datetime="{date}"></time></p><div class="field--name-field-article-date">{date}</div><div class="text-long field--name-field-article-resume">{body}</div>'
            query=parse_qs(urlsplit(url).query)
            if self.SITE=='bmleh-de-siteglobals':
                page=int(query.get('gtp',['_list%3D1'])[0].rsplit('%3D',1)[-1])-1
                self.first_list=c._START_URL+'?resultsPerPage=5'
            else:
                page=int(query.get('page',['0'])[0]);self.first_list=(c.LIST_URL if self.SITE=='ens-lyon-fr-lecole' else c._LIST_URL)+'?page=0'
            ids={0:range(1,6),1:range(6,9)}.get(page,[])
            cards=[]
            for n in ids:
                href=f'/report-{n}.html';date=getattr(self,'date_values',['2025-07-11T12:34:56Z']*8)[n-1]
                cards.append(f'<div class="c-searchteaser views-row"><h2 class="c-searchteaser__h"><a class="un-listings-box" href="{href}">Report {n}<time datetime="{date}"></time></a></h2><p class="c-searchteaser__meta"><time datetime="{date}"></time></p></div><article data-history-node-id="{n}"><a href="{href}">Report {n}</a></article>')
            next_link='<li class="pager__item--next"><a href="?page=1">Next</a></li>' if page==0 else ''
            return '<p class="c-search-resultsperpage__p">1 bis 5 von 8</p><a href="?gtp=42_list%253D2">Next</a>'+''.join(cards)+next_link
        self.fetch=Mock(side_effect=fetch)
        if self.LIST_MODULE:self.enterContext(patch.object(self.module,self.LIST_METHOD,self.fetch))
        else:setattr(c,self.LIST_METHOD,self.fetch)
        return c
    def test_zero_limit_performs_no_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()
    def test_cancel_after_final_record_is_observed(self):
        c=self.crawler();c.delivery_should_cancel=lambda:self.count()>=8
        with self.assertRaises(CrawlCancelled):c.crawl()
        self.assertEqual(self.count(),8)
    def test_listing_fetch_failure_is_reported(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**kw:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_detail_fetch_failure_is_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:None if re.search(r'/report-\d+\.html$',url) else original(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_repeated_source_page_is_not_successful_completion(self):
        c=self.crawler();original=self.fetch.side_effect;first={}
        def fetch(url,**kw):
            if '/report-' in url:return original(url,**kw)
            if not first:first['url']=url
            return original(first['url'],**kw)
        self.fetch.side_effect=fetch;self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_excluded_first_page_does_not_hide_later_records(self):
        self.excluded=set(range(1,6));self.assertEqual(self.crawler().crawl(),3);self.assertEqual(self.count(),3)
    def test_page_bound_retains_prefix_and_reports_incomplete(self):
        c=self.crawler()
        if self.SITE=='ungeneva-org-en':self.enterContext(patch.object(self.module,'_MAX_PAGES',1))
        elif self.SITE=='ens-lyon-fr-lecole':c.MAX_PAGES=1
        else:c._MAX_PAGES=1
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')

class BmlehCounts(EuropePressContract,unittest.TestCase):
    SITE='bmleh-de-siteglobals';CLASS='BmlehDeSiteglobalsCrawler';LIST_METHOD='_fetch';LIST_MODULE=False
    def test_german_grouped_source_total_is_not_truncated(self):
        self.assertEqual(self.module.BmlehDeSiteglobalsCrawler._total_results(self.module._make_soup('<p class="c-search-resultsperpage__p">1 bis 50 von 1.245</p>')),1245)

class EnsLyonCounts(EuropePressContract,unittest.TestCase):
    SITE='ens-lyon-fr-lecole';CLASS='EnsLyonFrLecoleCrawler';LIST_METHOD='_curl_get';LIST_MODULE=False
    def test_press_sidebar_PDF_wins_over_earlier_body_report_and_keeps_candidates(self):
        import json
        c=self.crawler();original=self.fetch.side_effect;papers=[];save=c._save_paper
        def fetch(url,**kw):
            raw=original(url,**kw)
            if '/report-' in url:
                raw+='<main><a href="https://reference.invalid/report.pdf">rapport</a></main><aside><div class="block-article-bloc"><a href="/press%20release.pdf">Télécharger le communiqué</a><a href="/annex.pdf">Annexe</a></div></aside>'
            return raw
        self.fetch.side_effect=fetch
        def persist(paper):papers.append(paper);return save(paper)
        c._save_paper=persist;self.assertEqual(c.crawl(limit=1),1)
        row=self.conn.execute('SELECT pdf_url,original_filename FROM documents').fetchone()
        values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
        self.assertEqual(values,('https://www.ens-lyon.fr/press%20release.pdf','press release.pdf'))
        meta=json.loads(papers[0]['metadata'])
        self.assertEqual(meta['source_pdf_candidates'],['https://reference.invalid/report.pdf','https://www.ens-lyon.fr/press%20release.pdf','https://www.ens-lyon.fr/annex.pdf'])
        self.assertEqual(meta['primary_pdf_binding'],'explicit_press_document')
        self.assertEqual(meta['source_press_pdf_urls'],['https://www.ens-lyon.fr/press%20release.pdf'])
        self.assertEqual(c.crawl(limit=1),1)
    def test_explicit_press_label_outside_article_does_not_override_article(self):
        c=self.crawler();s=c._make_soup('<nav><a href="/nav.pdf">Communiqué de presse</a></nav><main><a href="https://partner.invalid/release.pdf">Télécharger le communiqué</a></main>')
        all_pdfs,press=c._pdf_candidates(s)
        self.assertEqual(press,['https://partner.invalid/release.pdf']);self.assertEqual(len(all_pdfs),2)
    def test_decomposed_accent_and_duplicate_press_anchors_keep_source_order(self):
        c=self.crawler();s=c._make_soup('<main><a href="/a.pdf">COMMUNIQUE\u0301</a><a href="/a.pdf">communiqué de presse</a><a href="/b.pdf">Dossier de presse complet</a></main>')
        all_pdfs,press=c._pdf_candidates(s)
        self.assertEqual(all_pdfs,press);self.assertEqual(press,['https://www.ens-lyon.fr/a.pdf','https://www.ens-lyon.fr/b.pdf'])
    def test_unlabelled_candidates_remain_explicitly_unverified(self):
        c=self.crawler();s=c._make_soup('<main><a href="/paper.pdf">Scientific paper</a></main>')
        self.assertEqual(c._pdf_candidates(s),(['https://www.ens-lyon.fr/paper.pdf'],[]))
    def test_short_summary_recovers_ordered_article_paragraphs_without_footer(self):
        c=self.crawler();original=self.fetch.side_effect
        first='First source paragraph describing the published research findings. '*3
        second='Second source paragraph giving the evidence and implications. '*3
        def fetch(url,**kw):
            if '/report-' not in url:return original(url,**kw)
            return '<h1>Research report</h1><main><div class="field--name-field-article-resume">Short publisher summary.</div><div class="field--name-bp-text">'+first+'</div><div class="field--name-bp-text">'+second+'</div></main><footer><div class="field--name-bp-text">UNRELATED FOOTER '+('Navigation '*100)+'</div></footer>'
        self.fetch.side_effect=fetch
        for expected in (3,3,2,0):self.assertEqual(c.crawl(limit=3),expected)
        for r in self.conn.execute('SELECT abstract FROM documents'):
            self.assertEqual(r['abstract'] if isinstance(r,dict) else r[0],' '.join((first+second).split()))
    def test_external_body_field_does_not_promote_empty_article(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw) if '/report-' not in url else '<h1>Empty article</h1><main><div class="field--name-bp-text">Video</div></main><footer><div class="field--name-bp-text">'+('Unrelated navigation '*100)+'</div></footer>'
        self.assertEqual(c.crawl(),0);self.assertEqual(self.count(),0)
    def test_sufficient_summary_preserved_when_article_paragraphs_exist(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw)+'<main><div class="field--name-bp-text">'+('Longer body '*100)+'</div></main>'
        self.assertEqual(c.crawl(),8)
        for r in self.conn.execute('SELECT abstract FROM documents'):
            self.assertEqual(r['abstract'] if isinstance(r,dict) else r[0],('Original publisher research and publication evidence. '*10).strip())
    def test_source_publication_label_retains_raw_text_and_date_precision(self):
        self.date_values=['Date de publication 2025','Date de publication juillet 2025','Date de publication 31/02/2025','Date de publication 23/04/2026','Date de publication 23/04/2026 suffix','Date de publication 03/04/2025','Date de publication 2025-07-11','Date de publication 2025-07']
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_status,published_date_precision FROM documents ORDER BY seq_id').fetchall()
        actual=[tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]
        expected=[(None,'partial','year'),(None,'partial','month'),(None,'invalid','day'),('2026-04-23','normalized','day'),(None,'unrecognized',None),('2025-04-03','normalized','day'),('2025-07-11','normalized','day'),(None,'partial','month')]
        self.assertEqual(actual,[(value,raw,status,precision) for raw,(value,status,precision) in zip(self.date_values,expected)])

class UNGenevaCounts(EuropePressContract,unittest.TestCase):
    SITE='ungeneva-org-en';CLASS='UNGenevaEnCrawler';LIST_METHOD='_curl_get';LIST_MODULE=True
    def test_final_allowed_page_is_processed(self):
        c=self.crawler()
        with patch.object(self.module,'_MAX_PAGES',2):self.assertEqual(c.crawl(),8)
        self.assertFalse(c.delivery_source_failure)
    def test_entire_source_date_is_preserved(self):
        self.date_values=['2025','July 2025','2025-02-30','2025-07-11T12:34:56Z','2025-07-11 suffix','03/04/2025','July 11, 2025','2025-07']
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date_raw,published_date_status FROM documents ORDER BY seq_id').fetchall()
        values=[tuple(r.values()) if isinstance(r,dict) else tuple(r) for r in rows]
        self.assertEqual(values,list(zip(self.date_values,['partial','partial','invalid','normalized','unrecognized','ambiguous','normalized','partial'])))

for cls in (BmlehCounts,EnsLyonCounts,UNGenevaCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class EuropePressWorkerLifecycle(unittest.TestCase):
    def test_actual_worker_preserves_new_counts_cancel_and_failure_statuses(self):
        from delivery.db.schema import init_delivery_schema
        from delivery.worker import jobs,worker
        for cls in (BmlehCounts,EnsLyonCounts,UNGenevaCounts):
            with self.subTest(site=cls.SITE):
                case=cls(methodName='test_repeat_three_reaches_remainder_and_then_zero');case.BACKEND='postgres'
                try:
                    case.setUp();init_delivery_schema(case.conn)
                    for limit,cancel,expected in [(3,None,3),(3,None,3),(None,2,2),(None,None,0)]:
                        c=case.crawler();before=case.count();jid=jobs.enqueue_job(case.conn,case.SITE,mode='full',limit_n=limit)
                        job=jobs.claim_next_job(case.conn)
                        def should_cancel():
                            if cancel and case.count()>=before+cancel:jobs.cancel_job(case.conn,jid);return True
                            return False
                        returned=worker.run_job(case.conn,job,{case.SITE:lambda **kw:c},delay=0,should_cancel=should_cancel)
                        detail=jobs.job_detail(case.conn,jid)['job']
                        self.assertEqual((returned,detail['saved_count'],detail['status']),(expected,expected,'cancelled' if cancel else 'done'))
                        self.assertEqual(case.count()-before,expected)
                    c=case.crawler();case.fetch.side_effect=lambda *a,**kw:None
                    jid=jobs.enqueue_job(case.conn,case.SITE,mode='full')
                    self.assertEqual(worker.run_job(case.conn,jobs.claim_next_job(case.conn),{case.SITE:lambda **kw:c},delay=0),0)
                    detail=jobs.job_detail(case.conn,jid)['job'];self.assertEqual(detail['status'],'failed');self.assertIn('fetch_failed',detail['error']);self.assertEqual(case.count(),8)
                finally:case.doCleanups()
