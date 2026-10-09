"""Native EPrints JSON and NOAA citation HTML with actual persistence."""
import json
import os
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs,urlsplit
from tests.test_coupled_site_counts import CoupledContract

DATES=[('2025',None,'year','partial'),('2025-07',None,'month','partial'),
       ('2025-02-30',None,'day','invalid'),('2025-07-11T12:34:56Z','2025-07-11','day','normalized'),
       ('2025-07-11 suffix',None,None,'unrecognized'),('03/04/2025',None,'day','ambiguous'),
       ('2025/07/11','2025-07-11','day','normalized'),('20250711','2025-07-11','day','normalized')]


class RepositoryContract(CoupledContract):
    def test_raw_dates_retained(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,reversed(DATES)):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):self.assertEqual(values,(value,raw,precision,status))


class DorasCounts(RepositoryContract,unittest.TestCase):
    SITE='doras-dcu-ie-view';LIST_METHOD='_fetch_json_export'
    def crawler(self):
        c=self.configured(self.module.DorasDcuIeViewCrawler(self.conn))
        self.records=[{'eprintid':n,'title':f'Thesis {n}','abstract':'Research evidence. '*12,
                       'date':DATES[n-1][0],'datestamp':'2025-07-11T12:34:56Z',
                       'documents':[{'security':'public','format':'application/pdf','placement':1,'main':f'Thesis {n}.pdf'}]}
                      for n in range(1,9)]
        self.response=Mock();self.response.json.side_effect=lambda:self.records
        self.response.content=json.dumps(self.records).encode()
        c._session.get=Mock(return_value=self.response);c._PAGE_SIZE=3
        return c

    def test_deposit_timestamp_retained(self):
        self.assertEqual(self.crawler().crawl(),8)
        for row in self.conn.execute('SELECT listed_date,listed_date_raw FROM documents').fetchall():
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            self.assertEqual(values,('2025-07-11','2025-07-11T12:34:56Z'))

    def test_empty_export_is_not_fetch_failure(self):
        c=self.crawler();self.records=[]
        self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_source_failure)

    def test_failed_or_invalid_export_reported(self):
        for kind in ('fetch','shape','record'):
            c=self.crawler()
            if kind=='fetch':c._session.get.side_effect=RuntimeError('controlled source failure')
            else:self.records={} if kind=='shape' else [None]
            self.assertEqual(c.crawl(),0);self.assertTrue(c.delivery_source_failure);self.assertFalse(c.delivery_exhausted)

    def test_integer_and_numeric_string_ids_can_be_sorted_together(self):
        c=self.crawler();self.records[0]['eprintid']='1'
        self.assertEqual(c.crawl(),8);self.assertEqual(self.count(),8)

    def test_zero_limit_no_export_request(self):
        c=self.crawler();self.assertEqual(c.crawl(limit=0),0);c._session.get.assert_not_called()


class NoaaCounts(RepositoryContract,unittest.TestCase):
    SITE='repository-library-noaa-gov-view';LIST_METHOD='_curl_get'
    def crawler(self):
        c=self.configured(self.module.NOAARepositoryViewCrawler(self.conn))
        def fetch(url,**kw):
            maximum=getattr(self,'population',8)
            if url.startswith(c._BROWSE_URL):
                offset=int(parse_qs(urlsplit(url).query)['start'][0])
                return '<main>'+''.join(f'<a href="/view/noaa/{n}">Report {n}</a>' for n in (range(maximum,maximum-3,-1) if offset==0 else []))+'</main>'
            n=int(url.rsplit('/',1)[1])
            if not 1<=n<=maximum:return '<title>Page not found</title>'
            abstract='' if n in getattr(self,'excluded',set()) else 'Scientific evidence. '*12
            return f'<meta name="citation_title" content="Report {n}"><meta name="citation_publication_date" content="{DATES[(n-1)%8][0]}"><meta name="citation_abstract" content="'+abstract+'">'
        c._curl_get=self.fetch=Mock(side_effect=fetch);return c

    def test_many_known_sequential_ids_do_not_count_as_missing(self):
        self.population=68
        self.assertEqual(self.crawler().crawl(limit=66),66);self.assertEqual(self.count(),66)
        self.assertEqual(self.crawler().crawl(limit=2),2);self.assertEqual(self.count(),68)

    def test_many_excluded_sequential_ids_do_not_count_as_missing(self):
        self.population=68;self.excluded=set(range(3,69))
        self.assertEqual(self.crawler().crawl(),2);self.assertEqual(self.count(),2)

    def test_sequential_fetch_failure_stops_without_skipping_unknown_item(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:None if url==c._DETAIL_BASE+'5' else original(url,**kw)
        self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertNotIn(c._DETAIL_BASE+'4',[call.args[0] for call in self.fetch.call_args_list])

    def test_failed_listing_does_not_trigger_guessed_default_id_scan(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.assertEqual(self.fetch.call_count,1)

    def test_empty_listing_has_no_seed_for_id_scan(self):
        c=self.crawler();self.fetch.side_effect=lambda *a,**k:'<main></main>'
        self.assertEqual(c.crawl(),0);self.assertEqual(self.fetch.call_count,1)
        self.assertFalse(c.delivery_exhausted)

    def test_detail_failure_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(url,**kw) if url.startswith(c._BROWSE_URL) else None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_repeated_listing_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(c._BROWSE_URL+'?start=0' if url.startswith(c._BROWSE_URL) else url,**kw)
        self.assertEqual(c.crawl(),3);self.assertEqual(c.delivery_source_failure,'invalid_response')


for cls in (DorasCounts,NoaaCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg
