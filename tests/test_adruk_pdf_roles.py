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
    def test_publication_button_without_download_list_is_an_attachment(self):
        d=module._parse_detail(self.page('<a href="https://citation.invalid/background.pdf">Background</a><p class="button more"><strong><a href="https://publisher.invalid/report.pdf">Read full publication</a></strong></p>'))
        self.assertEqual(d['pdf_url'],'https://publisher.invalid/report.pdf')
        self.assertEqual(d['primary_pdf_binding'],'explicit_publication_button')
        self.assertEqual(d['reference_pdf_urls'],['https://citation.invalid/background.pdf'])
    def test_download_list_priority_and_report_button_variant_are_preserved(self):
        d=module._parse_detail(self.page('<p class="button"><a href="/fileadmin/button.pdf">Read publication</a></p>','<li class="download pdf"><a href="/fileadmin/download.pdf">Download report</a></li>'))
        self.assertEqual(d['attachment_pdf_urls'],['https://www.adruk.org/fileadmin/download.pdf','https://www.adruk.org/fileadmin/button.pdf'])
        self.assertEqual(d['pdf_url'],d['attachment_pdf_urls'][0])
        self.assertEqual(d['primary_pdf_binding'],'explicit_download_list')
    def test_button_outside_article_content_is_not_an_attachment(self):
        d=module._parse_detail(self.page('','<p class="button"><a href="/fileadmin/navigation.pdf">Navigation</a></p>'))
        self.assertIsNone(d['pdf_url'])
        self.assertEqual(d['reference_pdf_urls'],['https://www.adruk.org/fileadmin/navigation.pdf'])

    def test_poster_collection_keeps_every_poster_without_arbitrary_primary(self):
        body = '<h3>Poster presentations</h3><p><strong><a href="/files/a.pdf">Poster A</a></strong></p><p><strong><a href="/files/b.pdf">Poster B</a></strong></p>'
        d = module._parse_detail(self.page(body))
        self.assertIsNone(d['pdf_url'])
        self.assertEqual(d['collection_pdf_urls'],['https://www.adruk.org/files/a.pdf','https://www.adruk.org/files/b.pdf'])
        self.assertEqual(d['attachment_pdf_urls'],d['collection_pdf_urls'])
        self.assertEqual(d['reference_pdf_urls'],[])
        self.assertEqual(d['primary_pdf_binding'],'collection_without_primary_PDF')

    def test_poster_collection_stops_at_peer_heading_and_keeps_citations_separate(self):
        body = '<p><strong><a href="/before.pdf">Background</a></strong></p><h3>Poster presentations</h3><p><strong><a href="/poster.pdf">Poster</a></strong> cites <a href="/citation.pdf">a review</a></p><h3>Related research</h3><p><strong><a href="/after.pdf">Other research</a></strong></p>'
        d = module._parse_detail(self.page(body))
        self.assertEqual(d['attachment_pdf_urls'],['https://www.adruk.org/poster.pdf'])
        self.assertEqual(d['reference_pdf_urls'],['https://www.adruk.org/before.pdf','https://www.adruk.org/citation.pdf','https://www.adruk.org/after.pdf'])

    def test_explicit_primary_remains_ahead_of_poster_collection(self):
        d = module._parse_detail(self.page('<h3>Poster presentations</h3><p><strong><a href="/poster.pdf">Poster</a></strong></p>','<li class="download pdf"><a href="/proceedings.pdf">Proceedings</a></li>'))
        self.assertEqual(d['pdf_url'],'https://www.adruk.org/proceedings.pdf')
        self.assertEqual(d['attachment_pdf_urls'],['https://www.adruk.org/proceedings.pdf','https://www.adruk.org/poster.pdf'])

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
    def test_native_save_keeps_external_publication_button(self):
        c=self.case.crawler();old=self.case.fetch.side_effect
        def fetch(url,**kwargs):
            raw=old(url,**kwargs)
            if '/article/I1/' in url:raw=raw.replace('</div></div>','<p class="button"><a href="https://publisher.invalid/report.pdf">Read full publication</a></p></div></div>')
            return raw
        self.case.fetch.side_effect=fetch;self.assertEqual(c.crawl(limit=1),1)
        row=self.case.conn.execute('SELECT pdf_url FROM documents').fetchone()
        self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],'https://publisher.invalid/report.pdf')
    def test_annual_inline_identity_without_report_root_link_is_supported(self):
        c=self.case.crawler();old=self.case.fetch.side_effect
        c._pdf_bytes=lambda url:b'identical retained annual fixture bytes'
        self.case.fetch.side_effect=lambda url,**kw:fixtures.detail('P5',linked=False) if '/publication/P5/' in url else old(url,**kw)
        self.assertEqual(c.crawl(),8)
        row=self.case.conn.execute("SELECT pdf_url FROM documents WHERE post_number='P5'").fetchone();self.assertEqual(row['pdf_url'] if isinstance(row,dict) else row[0],fixtures.ROOT+'/fileadmin/Annual_Report_2025-26.pdf')

    def test_native_poster_collection_payload_does_not_assign_first_file_as_primary(self):
        import json
        c=self.case.crawler();old=self.case.fetch.side_effect;captured=[];save=c._save_paper
        def fetch(url,**kwargs):
            raw=old(url,**kwargs)
            if '/article/I1/' in url:raw=raw.replace('</div></div>','<h3>Poster presentations</h3><p><strong><a href="/poster.pdf">Poster title</a></strong></p></div></div>')
            return raw
        def capture(paper):captured.append(paper);return save(paper)
        self.case.fetch.side_effect=fetch;c._save_paper=capture;self.assertEqual(c.crawl(limit=1),1)
        row=self.case.conn.execute('SELECT pdf_url FROM documents').fetchone();self.assertIsNone(row['pdf_url'] if isinstance(row,dict) else row[0])
        meta=json.loads(captured[0]['metadata']);self.assertEqual(meta['source_collection_pdf_urls'],[fixtures.ROOT+'/poster.pdf']);self.assertEqual(meta['source_attachment_pdf_urls'],[fixtures.ROOT+'/poster.pdf'])

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires disposable owned PostgreSQL')
class AdrukPDFPersistencePostgres(AdrukPDFPersistence):BACKEND='postgres'
