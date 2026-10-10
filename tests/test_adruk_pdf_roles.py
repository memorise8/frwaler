"""Keep citation PDFs distinct from ADR's explicit article/report attachments."""
import importlib.util,os,unittest
from pathlib import Path
from tests import test_adruk_delivery_scopes as fixtures
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('adruk_pdf_roles',ROOT/'crawler/sites/custom/adruk-org-news-publications.py');module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class AdrukPDFRoles(unittest.TestCase):
    def page(self,body,attachments=''):
        return '<h1>Impact article</h1><div class="news single model-impact-cases"><div class="content"><p>Research evidence.</p>'+body+'</div><div class="wrapper"><ul>'+attachments+'</ul></div></div>'
    def test_explicit_download_wins_over_earlier_inline_citation(self):
        d=module._parse_detail(self.page('<a href="https://citation.invalid/reference.pdf">Background review</a>','<li class="download pdf"><a href="/fileadmin/article.pdf">Download article</a></li>'))
        self.assertEqual(d['pdf_url'],'https://www.adruk.org/fileadmin/article.pdf');self.assertEqual(d['reference_pdf_urls'],['https://citation.invalid/reference.pdf']);self.assertEqual(len(d['pdf_urls']),2)
    def test_inline_citation_is_retained_but_not_claimed_as_own_PDF(self):
        d=module._parse_detail(self.page('<a href="https://citation.invalid/reference.pdf">Background review</a>'))
        self.assertIsNone(d['pdf_url']);self.assertEqual(d['pdf_urls'],d['reference_pdf_urls']);self.assertEqual(d['primary_pdf_binding'],'no_explicit_attachment')
    def test_all_explicit_files_preserved_in_source_order(self):
        d=module._parse_detail(self.page('<a href="/fileadmin/b.pdf">See appendix</a>','<li class="download pdf"><a href="/fileadmin/a.pdf">Report</a></li><li class="download pdf"><a href="/fileadmin/b.pdf">Appendix</a></li>'))
        self.assertEqual(d['attachment_pdf_urls'],['https://www.adruk.org/fileadmin/a.pdf','https://www.adruk.org/fileadmin/b.pdf']);self.assertEqual(d['reference_pdf_urls'],[])
    def test_navigation_download_outside_article_is_not_an_attachment(self):
        d=module._parse_detail('<nav><li class="download pdf"><a href="/fileadmin/nav.pdf">Navigation</a></li></nav>'+self.page('<p>No article file.</p>'))
        self.assertIsNone(d['pdf_url']);self.assertEqual(d['pdf_urls'],[])
    def test_external_explicit_attachment_keeps_original_URL(self):
        d=module._parse_detail(self.page('','<li class="download pdf"><a href="https://publisher.invalid/report.pdf">Partner report</a></li>'))
        self.assertEqual(d['pdf_url'],'https://publisher.invalid/report.pdf')

class AdrukPDFPersistence(unittest.TestCase):
    BACKEND='sqlite'
    def setUp(self):
        self.case=fixtures.AdrukScopes(methodName='test_zero_limit_has_no_requests');self.case.BACKEND=self.BACKEND;self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.enterContext(patch('requests.Session.get', side_effect=AssertionError('unexpected public network in fixture test')))
    def test_native_save_uses_explicit_file_and_preserves_reference_in_payload(self):
        c=self.case.crawler();old=self.case.fetch.side_effect;captured=[];save=c._save_paper
        def fetch(url,**kwargs):
            raw=old(url,**kwargs)
            if '/article/I1/' in url:raw=raw.replace('</div></div>','<a href="https://citation.invalid/background.pdf">Background</a></div><ul><li class="download pdf"><a href="/fileadmin/own-I1.pdf">Own report</a></li></ul></div>')
            return raw
        self.case.fetch.side_effect=fetch
        def capture(paper):captured.append(paper);return save(paper)
        c._save_paper=capture;self.assertEqual(c.crawl(limit=1),1)
        row=self.case.conn.execute('SELECT pdf_url FROM documents').fetchone();self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],fixtures.ROOT+'/fileadmin/own-I1.pdf')
        import json
        meta=json.loads(captured[0]['metadata']);self.assertEqual(meta['source_reference_pdf_urls'],['https://citation.invalid/background.pdf']);self.assertEqual(meta['primary_pdf_binding'],'explicit_download_list')
        self.assertEqual(self.case.crawler().crawl(),7)
    def test_annual_identity_selects_report_instead_of_unrelated_download(self):
        c=self.case.crawler();old=self.case.fetch.side_effect
        def fetch(url,**kw):
            raw=old(url,**kw)
            if '/publication/P5/' in url:raw=raw.replace('</div></div>','</div><ul><li class="download pdf"><a href="/fileadmin/strategy.pdf">Strategy</a></li></ul></div>')
            return raw
        self.case.fetch.side_effect=fetch;self.assertEqual(c.crawl(),8)
        row=self.case.conn.execute("SELECT pdf_url FROM documents WHERE post_number='P5'").fetchone();self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],fixtures.ANNUAL_PDF)
    def test_annual_inline_identity_without_report_root_link_is_supported(self):
        c=self.case.crawler();old=self.case.fetch.side_effect
        c._pdf_bytes=lambda url:b'identical retained annual fixture bytes'
        self.case.fetch.side_effect=lambda url,**kw:fixtures.detail('P5',linked=False) if '/publication/P5/' in url else old(url,**kw)
        self.assertEqual(c.crawl(),8)
        row=self.case.conn.execute("SELECT pdf_url FROM documents WHERE post_number='P5'").fetchone();self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],fixtures.ROOT+'/fileadmin/Annual_Report_2025-26.pdf')

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AdrukPDFPersistencePostgres(AdrukPDFPersistence):BACKEND='postgres'
