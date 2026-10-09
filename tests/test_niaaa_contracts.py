"""NIAAA native news HTML fixtures with actual SQLite/PostgreSQL writes."""
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit
from tests.test_coupled_site_counts import CoupledContract
from crawler.document_dates import normalize_date

DATES=[('2025',None,'year','partial'),('July 2025',None,'month','partial'),
       ('2025-02-30',None,'day','invalid'),('2025-07-11T12:34:56Z','2025-07-11','day','normalized'),
       ('2025-07-11 wrong suffix',None,None,'unrecognized'),('03/04/2025',None,'day','ambiguous'),
       ('Friday, July 11, 2025','2025-07-11','day','normalized'),
       ('Friday, February 30, 2025',None,'day','invalid')]


class NiaaaCounts(CoupledContract,unittest.TestCase):
    SITE='niaaa-nih-gov-news-events';LIST_METHOD='_curl_get';LIST_MODULE=True
    def crawler(self):
        c=self.configured(self.module.NIAAANewsEventsCrawler(self.conn))
        def fetch(url,*args,**kw):
            if '?page=' in url:
                page=int(parse_qs(urlsplit(url).query)['page'][0])
                return '<main>'+''.join(f'<a href="/news-events/announcement/research-{n}">Research {n}</a>' for n in {0:range(1,6),1:range(6,9)}.get(page,[]))+'</main>'
            n=int(url.rsplit('-',1)[1]);raw=DATES[n-1][0]
            stamp=f'<time datetime="{raw}"></time>' if n<=6 else '<div>'+raw+'</div>'
            return f'<main><h1>Research {n}</h1><span>Announcement</span>'+stamp+'<p>'+('Scientific evidence and results. '*12)+'</p></main>'
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch));return c

    def test_raw_structured_and_legacy_date_headers_preserved(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):self.assertEqual(values,(value,raw,precision,status))

    def test_date_inside_article_prose_is_not_publication_date(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,*a,**k:original(url,*a,**k) if '?page=' in url else '<main><h1>Research</h1><p>Our study started on July 11, 2025. '+('Scientific research. '*12)+'</p></main>'
        self.assertEqual(c.crawl(),8)
        for row in self.conn.execute('SELECT published_date,published_date_status FROM documents').fetchall():
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            self.assertEqual(values,(None,'missing'))

    def test_failed_listing_reported(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')

    def test_failed_detail_or_missing_main_reported(self):
        for content in (None,'<html><h1>Unavailable</h1></html>'):
            c=self.crawler();original=self.fetch.side_effect
            self.fetch.side_effect=lambda url,*a,**k:original(url,*a,**k) if '?page=' in url else content
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_both_listing_parsers_failing_is_not_empty_success(self):
        c=self.crawler()
        with patch.object(self.module,'_make_soup',return_value=None),patch.object(c,'_parse_listing_regex',return_value=[]):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_repeated_listing_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,*a,**k:original(self.module._LIST_URL+'?page=0' if '?page=' in url else url,*a,**k)
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class NiaaaPostgres(NiaaaCounts):
    BACKEND='postgres'


class WeekdayDateTests(unittest.TestCase):
    def test_complete_calendar_date_only(self):
        for raw,value,status in [('Friday, July 11, 2025','2025-07-11','normalized'),
                                 ('Friday, February 30, 2025',None,'invalid'),
                                 ('Friday, July 11, 2025 extra',None,'unrecognized'),
                                 ('FakeDay, July 11, 2025',None,'unrecognized')]:
            with self.subTest(raw=raw):
                result=normalize_date(raw);self.assertEqual(result['value'],value);self.assertEqual(result['status'],status);self.assertEqual(result['raw'],raw)
