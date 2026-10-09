"""Native Klug HTML and PubMed XML fixtures with actual database persistence."""
import os
import unittest
from unittest.mock import patch
from tests.test_coupled_site_counts import CoupledContract
from crawler.document_dates import normalize_date

DATE_XML=['<PubDate><Year>2025</Year></PubDate>',
          '<PubDate><Year>2025</Year><Month>Jul</Month></PubDate>',
          '<PubDate><Year>2025</Year><Month>02</Month><Day>30</Day></PubDate>',
          '<PubDate><Year>2025</Year><Month>07</Month><Day>11</Day></PubDate>',
          '<PubDate><Year>2025</Year><Month>Unknown</Month><Day>11</Day></PubDate>',
          '<PubDate><MedlineDate>2025 Jan-Feb</MedlineDate></PubDate>',
          '<ArticleDate><Year>2025</Year><Month>07</Month><Day>11</Day></ArticleDate>', '']
DATES=[('2025',None,'year','partial'),('2025 Jul',None,'month','partial'),
       ('2025 02 30',None,'day','invalid'),('2025 07 11','2025-07-11','day','normalized'),
       ('2025 Unknown 11',None,None,'unrecognized'),('2025 Jan-Feb',None,None,'unrecognized'),
       ('2025 07 11','2025-07-11','day','normalized'),('May 2025',None,'month','partial')]


class MrcLmbCounts(CoupledContract,unittest.TestCase):
    SITE='www2-mrc-lmb-cam-ac-uk-research';LIST_METHOD='_curl';LIST_MODULE=True

    def crawler(self):
        c=self.configured(self.module.MrcLmbResearchCrawler(self.conn));m=self.module
        self.ajax_pages=[]
        def fetch(url,method='GET',data=None,**kw):
            if url.startswith('https://eutils.ncbi.nlm.nih.gov'):
                n=int(url.rsplit('=',1)[1])-1000
                abstract='' if n in getattr(self,'excluded',set()) else '<Abstract><AbstractText>'+('Research findings. '*12)+'</AbstractText></Abstract>'
                return f'<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>{1000+n}</PMID><Article>'+DATE_XML[n-1]+abstract+'</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>'
            if url==m.LISTING_URL:page=1
            else:
                self.assertEqual(url,m.AJAX_URL);self.assertEqual(method,'POST')
                page=int(data.get('frm_page') or data.get('page') or data['frm_page_order[2]']);self.ajax_pages.append(page)
            return '<main>'+''.join(f'<div class="wp-block-klug-publication"><a class="klug-publication__title" href="https://pubmed.ncbi.nlm.nih.gov/{1000+n}/">Research {n}</a><span class="klug-publication__journal"><strong>Journal</strong> May 2025</span></div>' for n in {1:range(1,6),2:range(6,9)}.get(page,[]))+'</main>'
        self.fetch=self.enterContext(patch.object(m,'_curl',side_effect=fetch));return c

    def test_all_known_first_page_does_not_hide_next_page(self):
        self.assertEqual(self.crawler().crawl(limit=5),5)
        self.assertEqual(self.crawler().crawl(limit=3),3);self.assertEqual(self.count(),8)

    def test_all_excluded_first_page_does_not_hide_next_page(self):
        self.excluded=set(range(1,6));self.assertEqual(self.crawler().crawl(),3);self.assertEqual(self.count(),3)

    def test_raw_xml_precision_and_listing_month_preserved(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):self.assertEqual(values,(value,raw,precision,status,None,'May 2025'))

    def test_failed_listing_and_ajax_reported(self):
        for initial in (True,False):
            c=self.crawler();original=self.fetch.side_effect
            self.fetch.side_effect=lambda url,**kw:None if url==(self.module.LISTING_URL if initial else self.module.AJAX_URL) else original(url,**kw)
            c.crawl();self.assertEqual(c.delivery_source_failure,'fetch_failed');self.assertFalse(c.delivery_exhausted)

    def test_failed_pubmed_fetch_or_xml_reported(self):
        for content in (None,'<html>Unavailable</html>','<PubmedArticleSet>'):
            c=self.crawler();original=self.fetch.side_effect
            self.fetch.side_effect=lambda url,**kw:content if 'eutils.ncbi.nlm.nih.gov' in url else original(url,**kw)
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_zero_limit_performs_no_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()

    def test_repeated_page_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(self.module.LISTING_URL) if url==self.module.AJAX_URL else original(url,**kw)
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.assertFalse(c.delivery_exhausted)

    def test_xml_date_gaps_do_not_shift_components(self):
        for xml,expected in [('<Year>2025</Year><Day>11</Day>','2025 ? 11'),
                             ('<Month>Jul</Month><Day>11</Day>','? Jul 11'),
                             ('<Year>2025</Year><Month>Jul</Month><Day>11</Day>','2025 Jul 11')]:
            value=self.module._parse_pubmed_xml('<PubmedArticleSet><PubmedArticle><PubDate>'+xml+'</PubDate></PubmedArticle></PubmedArticleSet>')
            self.assertEqual(value['published_date'],expected)
            if '?' in expected:self.assertIsNone(normalize_date(expected)['value'])


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class MrcLmbPostgres(MrcLmbCounts):
    BACKEND='postgres'


class YearFirstNamedDateTests(unittest.TestCase):
    def test_unambiguous_year_first_components(self):
        for raw,value,precision,status in DATES+[
            ('2025 Jul 11','2025-07-11','day','normalized'),('2025 13',None,'month','invalid'),
            ('0000 Jul',None,'month','invalid'),('2025 ? 11',None,None,'unrecognized'),
            ('2025 Jul 11 extra',None,None,'unrecognized')]:
            with self.subTest(raw=raw):self.assertEqual(normalize_date(raw),dict(raw=raw,value=value,precision=precision,status=status))
