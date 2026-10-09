"""Controlled HTML/RSS/N-Triples through native mapping and actual persistence."""
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from tests.test_coupled_site_counts import CoupledContract
from tests.test_statbel_contracts import DATES


class DataGovBeCounts(CoupledContract, unittest.TestCase):
    SITE='data-gov-be-nl';LIST_METHOD='_curl_get'

    def crawler(self):
        c=self.configured(self.module.DataGovBeNlCrawler(self.conn));m=self.module
        def fetch(url,**kw):
            if url==m._RSS_URL:
                return '<rss><channel>'+''.join(f'<item><link>https://data.gov.be/nl/datasets/record-{n}</link><guid>{n+1000}</guid><pubDate>Fri, 11 Jul 2025 12:34:56 GMT</pubDate></item>' for n in range(1,9))+'</channel></rss>'
            if url.startswith(m._LIST_URL):
                page=int(parse_qs(urlsplit(url).query).get('page',['0'])[0])
                return '<main>'+''.join(f'<article class="node--type-dataset"><a href="/nl/datasets/record-{n}">Report {n}</a><div class="field--name-body">'+('Listing description. '*12)+'</div></article>' for n in {0:range(1,6),1:range(6,9)}.get(page,[]))+('<a rel="next" href="?page=1">Next</a>' if page==0 else '')+'</main>'
            if url.startswith(m._LDF_URL):
                q=parse_qs(urlsplit(url).query)
                subject=q.get('subject',[None])[0]
                if subject is None:
                    slug=json.loads(q['object'][0]);n=int(slug.rsplit('-',1)[1]);subject=f'https://fixture.invalid/dataset/{n}'
                    return f'<{subject}> <{m.DCT_IDENTIFIER}> "record-{n}" .'
                n=int(subject.rsplit('/',1)[1])
                fields={m.DCT_IDENTIFIER:f'record-{n}',m.DCT_TITLE:f'Report {n}',m.DCT_DESCRIPTION:'Detailed source description. '*12,m.DCT_ISSUED:DATES[n-1][0]}
                return '\n'.join(f'<{subject}> <{key}> {json.dumps(value)} .' for key,value in fields.items())
            raise AssertionError('unexpected fixture URL '+url)
        c._curl_get=self.fetch=unittest.mock.Mock(side_effect=fetch)
        self.enterContext(patch.object(c,'_get_page',side_effect=AssertionError('No live browser in fixtures')))
        return c

    def test_raw_graph_and_rss_dates_preserved(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):self.assertEqual(values,(value,raw,precision,status,'2025-07-11','Fri, 11 Jul 2025 12:34:56 GMT'))

    def test_failed_listing_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:None if url.startswith(self.module._LIST_URL) else original(url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')

    def test_repeated_listing_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kw:original(self.module._LIST_URL if url.startswith(self.module._LIST_URL) else url,**kw)
        self.assertEqual(c.crawl(),5);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_failed_list_parser_reported(self):
        c=self.crawler();original=c._make_soup
        with patch.object(c,'_make_soup',side_effect=lambda raw,**kw:original(raw,**kw) if kw.get('prefer_xml') else None):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_failed_graph_is_not_silently_successful_enrichment(self):
        for graph in (None,'<html>Unavailable</html>'):
            c=self.crawler();original=self.fetch.side_effect
            self.fetch.side_effect=lambda url,**kw:graph if url.startswith(self.module._LDF_URL) else original(url,**kw)
            c.crawl();self.assertEqual(c.delivery_source_failure,'item_failed');self.assertFalse(c.delivery_exhausted)

    def test_rss_failure_not_silent(self):
        for rss in (None,'<html>Unavailable</html>'):
            c=self.crawler();original=self.fetch.side_effect
            self.fetch.side_effect=lambda url,**kw:rss if url==self.module._RSS_URL else original(url,**kw)
            c.crawl();self.assertEqual(c.delivery_source_failure,'item_failed')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class DataGovBePostgres(DataGovBeCounts):
    BACKEND='postgres'
