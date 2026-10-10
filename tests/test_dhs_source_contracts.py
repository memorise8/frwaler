"""DHS table date roles, cancellation, and persistence on both supported backends."""
import os
import unittest
from unittest.mock import patch

from crawler.document_dates import normalize_document_dates
from tests.test_coupled_site_counts import CoupledContract

DATES = ['01/22/2026', '01/23/2026', '03/04/2025', '',
         '02/28/2025', '12/31/2025', '11/19/2025', '07/11/2025']
NORMALIZED = ['2026-01-22', '2026-01-23', '2025-03-04', None,
              '2025-02-28', '2025-12-31', '2025-11-19', '2025-07-11']


def table(ids):
    return '<table aria-label="Files">'+''.join(
        '<tr><th role="rowheader" data-sort-value="Report '+str(n)+'">'
        f'<a href="https://www.dhs.gov/sites/default/files/report-{n}.pdf">Report {n}</a>'
        f'</th><td>PDF</td><td>10 KB</td><td>{DATES[n-1]}</td></tr>' for n in ids
    )+'</table>'


class DHSContract(CoupledContract):
    LIST_METHOD = '_fetch_playwright'
    LIST_MODULE = True

    def crawler(self):
        cls = (self.module.DHSCongressionalAppropCrawler if self.APPROPRIATIONS
               else self.module.DHSPublicationCrawler)
        c = self.configured(cls(self.conn, delay=0))
        def fetch(url, **kwargs):
            if not self.APPROPRIATIONS:
                return table(range(1, 9))
            if url == self.module._START_URL:
                return ''.join(f'<a href="/publication/{year}-dhs-congressional-appropriations-reports">{year}</a>'
                               for year in (2026, 2025))
            return table(range(1, 5) if '/2026-' in url else range(5, 9))
        self.fetch = self.enterContext(patch.object(self.module, '_fetch_playwright', side_effect=fetch))
        return c

    def test_table_date_raw_and_role_survive_database_save(self):
        self.assertEqual(self.crawler().crawl(), 8)
        rows = self.conn.execute('SELECT published_date,published_date_raw,listed_date,listed_date_raw FROM documents ORDER BY seq_id').fetchall()
        for row, raw, normalized in zip(rows, DATES, NORMALIZED):
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            self.assertEqual(values, (None, None, normalized, raw or None))

    def test_missing_or_unparseable_main_page_is_failure_not_empty(self):
        for raw, reason in [(None, 'fetch_failed'), ('<main>Unrecognized page</main>', 'invalid_response')]:
            with self.subTest(raw=raw):
                c = self.crawler(); self.fetch.side_effect = lambda *a, **kw: raw
                self.assertEqual(c.crawl(), 0)
                self.assertEqual(c.delivery_source_failure, reason)
                self.assertFalse(c.delivery_exhausted)

    def test_cancellation_after_main_response_prevents_further_work(self):
        c = self.crawler(); original = self.fetch.side_effect; cancelled = [False]
        c.delivery_should_cancel = lambda: cancelled[0]
        def fetch(*args, **kwargs):
            result = original(*args, **kwargs); cancelled[0] = True; return result
        self.fetch.side_effect = fetch
        from crawler.base_crawler import CrawlCancelled
        with self.assertRaises(CrawlCancelled): c.crawl()
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(self.count(), 0)

    def test_reviewed_MDY_rule_does_not_invent_partial_dates(self):
        for raw, value, status in [('2025', None, 'partial'), ('02/30/2025', None, 'invalid'),
                                   ('03/04/2025', '2025-03-04', 'normalized')]:
            result = normalize_document_dates({'site_id': self.SITE, 'listed_date': raw})
            self.assertEqual(result['listed_date_raw'], raw)
            self.assertEqual((result['listed_date'], result['listed_date_status']), (value, status))
        unrelated = normalize_document_dates({'site_id': 'unreviewed-dhs-like-site', 'listed_date': '03/04/2025'})
        self.assertIsNone(unrelated['listed_date'])
        self.assertEqual(unrelated['listed_date_status'], 'ambiguous')


class DHSCongressional(DHSContract, unittest.TestCase):
    SITE = 'dhs-gov-congressional-approp'
    APPROPRIATIONS = True

    def test_failed_year_page_is_not_silently_excluded(self):
        c = self.crawler(); original = self.fetch.side_effect
        self.fetch.side_effect = lambda url, **kw: None if '/2025-' in url else original(url, **kw)
        self.assertEqual(c.crawl(), 4)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')
        self.assertFalse(c.delivery_exhausted)
        self.assertEqual(self.crawler().crawl(), 4)
        self.assertEqual(self.count(), 8)


class DHSConference(DHSContract, unittest.TestCase):
    SITE = 'dhs-gov-publication'
    APPROPRIATIONS = False

    def test_title_fallback_uses_visible_link_text(self):
        raw = table([1]).replace(' data-sort-value="Report 1"', '')
        records = self.module._parse_listing_page(raw)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['title'], 'Report 1')


for cls in (DHSCongressional, DHSConference):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
