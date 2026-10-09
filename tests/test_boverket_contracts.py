"""Actual RSS/detail parsers and SQLite/PostgreSQL; controlled source only."""
import os
import unittest
from unittest.mock import patch
from tests.test_coupled_site_counts import CoupledContract
from crawler.document_dates import normalize_date

DATES = [('Fri, 11 Jul 2025 12:34:56 +0200', '2025-07-11', 'day', 'normalized'),
         ('2025', None, 'year', 'partial'),
         ('Fri, 30 Feb 2025 12:34:56 GMT', None, 'day', 'invalid'),
         ('11 Xxx 2025', None, None, 'unrecognized'),
         ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
         ('Fri, 11 Jul 2025 12:34:56 +0200 garbage', None, None, 'unrecognized'),
         ('03/04/2025', None, 'day', 'ambiguous'),
         ('July 2025', None, 'month', 'partial')]


class BoverketCounts(CoupledContract, unittest.TestCase):
    SITE='boverket-se-en'; LIST_METHOD='_curl_get'; LIST_MODULE=True

    def crawler(self):
        c=self.configured(self.module.BoverketSeEnCrawler(self.conn))
        def fetch(url):
            if url==self.module._RSS_URL:
                return '<rss><channel>'+''.join(
                    f'<item><guid>guid-{n}</guid><link>https://www.boverket.se/en/publications/{n}/</link><title>Report {n}</title><description>Summary</description><pubDate>{DATES[n-1][0] if n!=2 else ""}</pubDate></item>'
                    for n in range(1,9))+'</channel></rss>'
            return '<div class="main-body"><p>'+('Research report findings. '*20)+'</p></div><div class="publication-metadata"><div class="metadata-row"><div class="metadata-label">Year:</div><div class="metadata-value">2025</div></div></div><a rel="attachment" href="/globalassets/report.pdf">PDF</a>'
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch))
        return c

    def test_raw_dates_and_year_precision(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):self.assertEqual(values,(value,raw,precision,status))

    def test_zero_limit_performs_no_fetch(self):
        c=self.crawler()
        self.assertEqual(c.crawl(limit=0),0);self.fetch.assert_not_called()

    def test_failed_rss_is_not_empty_success(self):
        c=self.crawler();self.fetch.side_effect=lambda url:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_malformed_or_non_rss_is_reported(self):
        for raw in ('<rss>', '<html><body>Unavailable</body></html>', '<rss/>'):
            c=self.crawler();self.fetch.side_effect=lambda url:raw
            self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'invalid_response')

    def test_valid_empty_feed_has_no_failure_or_whole_site_claim(self):
        c=self.crawler();self.fetch.side_effect=lambda url:'<rss><channel/></rss>'
        self.assertEqual(c.crawl(),0);self.assertFalse(c.delivery_source_failure)
        self.assertFalse(c.delivery_exhausted)

    def test_failed_detail_is_reported(self):
        c=self.crawler();original=self.fetch.side_effect
        self.fetch.side_effect=lambda url:original(url) if url==self.module._RSS_URL else None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_detail_parser_failure_is_reported(self):
        c=self.crawler()
        with patch.object(self.module,'_make_soup',return_value=None):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'item_failed')


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class BoverketPostgres(BoverketCounts):
    BACKEND='postgres'


class RssDateContract(unittest.TestCase):
    def test_whole_rfc_string_and_calendar_validation(self):
        for raw,value,precision,status in DATES:
            with self.subTest(raw=raw):
                self.assertEqual(normalize_date(raw),dict(value=value,raw=raw,precision=precision,status=status))

    def test_bad_clock_or_offset_rejected(self):
        for raw in ('Fri, 11 Jul 2025 25:00:00 GMT', 'Fri, 11 Jul 2025 12:00:00 +2500', 'Fri, 11 Jul 2025 12:00:00 +0260'):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_date(raw)['value'])
