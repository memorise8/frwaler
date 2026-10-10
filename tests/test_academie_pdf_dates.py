"""Publication precision, event exclusion, raw retention and native persistence."""
import os
import unittest
from crawler.academie_pdf_dates import publication_date_evidence
from crawler.document_dates import normalize_document_dates
from tests.test_academie_press_sections import AcademieSections

SITE='academie-sciences-fr-espace-presse'


class PDFDateRoles(unittest.TestCase):
    def test_french_and_english_printed_dates_keep_original_values(self):
        for text,raw,date in [
            ('COMMUNIQUÉ DE PRESSE\nLe 3 juin 2026\nTitle', 'Le 3 juin 2026','2026-06-03'),
            ('Title\nParis, le 6 octobre 2026 – Original source statement.', 'Paris, le 6 octobre 2026','2026-10-06'),
            ('COMMUNIQUÉ DE PRESSE\nMard i 4 février 2025\nTitle','Mard i 4 février 2025','2025-02-04'),
            ('Executive Committee\nJune 26, 2025\nStatement','June 26, 2025','2025-06-26'),
            ('Document title\n2 juin 2025\nOriginal source body','2 juin 2025','2025-06-02'),
            ('1er avril 2025\nCOMMUNIQUÉ DE PRESSE\nTitle','1er avril 2025','2025-04-01'),
        ]:
            with self.subTest(text=text):
                e=publication_date_evidence(text);self.assertEqual(e['published_date_raw'],raw)
                d=normalize_document_dates(dict(site_id=SITE,published_date=raw))
                self.assertEqual(d['published_date_raw'],raw);self.assertEqual(d['published_date'],date)

    def test_partial_issue_month_never_becomes_portal_day(self):
        for text,raw in [('Communiqué de presse\nmai 2025\nPrize award','mai 2025'),
                         ('Episode 3 : Research\nSpecific topic (décembre 2025)\nEvidence','décembre 2025')]:
            e=publication_date_evidence(text);self.assertEqual(e['published_date_raw'],raw)
            d=normalize_document_dates(dict(site_id=SITE,published_date=raw,listed_date='15/12/2025'))
            self.assertIsNone(d['published_date']);self.assertEqual(d['published_date_status'],'partial')
            self.assertEqual(d['published_date_precision'],'month');self.assertEqual(d['published_date_raw'],raw)

    def test_event_body_citation_later_page_and_missing_date_are_not_publication(self):
        for text in ['DOSSIER DE PRESSE\nAward ceremony\nMardi 27 janvier 2026\nInstitut de France',
                     'Report title\nA previous report appeared on 14 janvier 2025.',
                     'White paper without date\fCOMMUNIQUÉ DE PRESSE\nLe 4 mai 2026',
                     'COMMUNIQUÉ DE PRESSE\nTitle\nThe event will take place on 17 juin 2025.']:
            with self.subTest(text=text):self.assertIsNone(publication_date_evidence(text)['published_date_raw'])

    def test_known_opinion_footer_and_ambiguous_headers(self):
        self.assertEqual(publication_date_evidence('Opinion body\n1\nAvis de l’Académie des sciences sur la politique énergétique.\n8 avril 2025')['published_date_raw'],'8 avril 2025')
        ambiguous=publication_date_evidence('COMMUNIQUÉ DE PRESSE\nLe 3 juin 2026\nLe 4 juin 2026')
        self.assertEqual(ambiguous['state'],'ambiguous');self.assertIsNone(ambiguous['published_date_raw'])
        self.assertEqual(len(ambiguous['candidates']),2)

    def test_invalid_printed_calendar_date_is_preserved(self):
        e=publication_date_evidence('COMMUNIQUÉ DE PRESSE\nLe 31 février 2025\nTitle')
        d=normalize_document_dates(dict(site_id=SITE,published_date=e['published_date_raw']))
        self.assertEqual(d['published_date_raw'],'Le 31 février 2025');self.assertEqual(d['published_date_status'],'invalid');self.assertIsNone(d['published_date'])


class PDFDatePersistence(AcademieSections):
    def test_native_title_only_PDF_is_incomplete_not_a_fabricated_body(self):
        c=self.crawler()
        c._extract_pdf_text=lambda *a:'LES FEMMES (ET LES FILLES) EN SCIENCES\n\fLes dernières parutions\n\f'
        self.assertEqual(c.crawl(),0);self.assertEqual(self.rows(),[])
        self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_native_PDF_dates_preserve_month_and_list_raw_without_fallback(self):
        c=self.crawler()
        c._extract_pdf_text=lambda *a:'COMMUNIQUÉ DE PRESSE\nmai 2025\nResearch source title\n'+'Original research evidence and explanations. '*15
        self.assertEqual(c.crawl(),6)
        for r in self.rows():
            if r['meta_url'].endswith('.pdf'):
                self.assertIsNone(r['published_date']);self.assertEqual(r['published_date_raw'],'mai 2025')
                if not r['meta_url'].endswith('dossier.pdf'):
                    self.assertEqual(r['listed_date_raw'],'31/01/2025');self.assertEqual(r['listed_date'],'2025-01-31')
        before=self.rows();self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.rows(),before)

    def test_native_undated_PDF_does_not_copy_listed_date(self):
        self.assertEqual(self.crawler().crawl(),6)
        for r in self.rows():
            if r['meta_url'].endswith('.pdf'):
                self.assertIsNone(r['published_date']);self.assertIsNone(r['published_date_raw'])


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'TEST_PG_DSN not configured')
class PDFDatePersistencePostgres(PDFDatePersistence):
    BACKEND='postgres'
