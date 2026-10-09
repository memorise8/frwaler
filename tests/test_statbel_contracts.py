"""Native Statbel parsers, controlled public HTML and actual database writes."""
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from tests.test_coupled_site_counts import CoupledContract

DATES=[('2025',None,'year','partial'),('2025-07',None,'month','partial'),
       ('2025-02-30',None,'day','invalid'),('2025-07-11T23:45:01+02:00','2025-07-11','day','normalized'),
       ('2025-07-11 bad suffix',None,None,'unrecognized'),('03/04/2025',None,'day','ambiguous'),
       ('20250711','2025-07-11','day','normalized'),('11 July 2025','2025-07-11','day','normalized')]


class StatbelNl(CoupledContract, unittest.TestCase):
    SITE='statbel-fgov-be-nl';LIST_METHOD='_curl_get';LIST_MODULE=True
    ENGLISH=False

    def crawler(self):
        c=self.configured(self.module.StatbelFgovBeNlCrawler(self.conn))
        self.list_calls=[]
        def fetch(jar,url,**kw):
            if url.startswith(c.NL_LIST_URL+'?') or url.startswith(c.EN_LIST_URL+'?'):
                self.list_calls.append(url)
                if self.ENGLISH and url.startswith(c.NL_LIST_URL+'?'):return None
                page=int(parse_qs(urlsplit(url).query)['page'][0])
                numbers={0:range(1,3),1:range(3,9)}.get(page,[])
                lang='en' if self.ENGLISH else 'nl'
                return ('<main>'+''.join(f'<div class="views-row"><div class="views-field-title"><a href="/{lang}/research-{n}">Report {n}</a></div><time datetime="2025-07-11T10:00:00Z"></time></div>' for n in numbers)+'</main>').encode()
            n=int(url.rsplit('-',1)[1])
            if '/en/' in url:
                return f'<html><head><link hreflang="nl" href="https://statbel.fgov.be/nl/research-{n}"></head></html>'.encode()
            return (f'<h1>Report {n}</h1><time datetime="{DATES[n-1][0]}"></time><link href="/node/{n+1000}"><div class="field--name-body">'+('Research findings with evidence. '*12)+'</div><a href="/report.pdf">Download</a>').encode()
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch))
        return c

    def test_new_limit_applies_across_page_boundary(self):
        self.assertEqual(self.crawler().crawl(limit=3),3);self.assertEqual(self.count(),3)

    def test_zero_limit_performs_no_fetch(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()

    def test_failed_listings_report_failure(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_parser_failure_is_not_empty_source(self):
        c=self.crawler()
        with patch.object(self.module,'_make_soup',return_value=None):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_repeated_listing_stops_with_failure(self):
        c=self.crawler();original=self.fetch.side_effect
        def repeating(jar,url,**kw):
            if '?page=' in url:url=url.split('?')[0]+'?page=0'
            return original(jar,url,**kw)
        self.fetch.side_effect=repeating
        self.assertEqual(c.crawl(),2)
        self.assertEqual(c.delivery_source_failure,'invalid_response')
        self.assertLessEqual(len(self.list_calls),3)

    def test_failed_detail_reports_failure(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda jar,url,**kw:original(jar,url,**kw) if '?page=' in url else None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_raw_dates_and_identity_survive_repeat(self):
        self.assertEqual(self.crawler().crawl(),8);before=self.identities()
        self.assertEqual(set(before),{f'research-{n}' for n in range(1,9)})
        self.assertEqual(self.crawler().crawl(),0);self.assertEqual(self.identities(),before)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):self.assertEqual(values,(value,raw,precision,status,'2025-07-11T10:00:00Z'))

    def test_midstream_nl_failure_does_not_skip_to_unequivalent_en_page(self):
        if self.ENGLISH:return
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda jar,url,**kw:None if url==c.NL_LIST_URL+'?page=1' else original(jar,url,**kw)
        self.assertEqual(c.crawl(),2);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.assertFalse(any(url.startswith(c.EN_LIST_URL) for url in self.list_calls))


class StatbelEn(StatbelNl):
    ENGLISH=True


for cls in (StatbelNl,StatbelEn):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg
