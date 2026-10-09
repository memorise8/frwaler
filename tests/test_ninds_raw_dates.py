"""Actual NINDS HTML parsers and real persistence must preserve date precision."""
import os
import unittest
from unittest.mock import patch

from tests.test_delivery_site_new_counts import SiteContract


class NindsRawDates(unittest.TestCase):
    SITE = 'ninds-nih-gov-publications'
    BACKEND = 'sqlite'
    setUp = SiteContract.setUp

    def test_source_month_timestamp_invalid_and_ambiguous_dates_survive_repeats(self):
        cases = [('07/2025', None, None, 'month', 'partial'),
                 ('13/2025', None, None, 'month', 'invalid'),
                 ('07/2025', '2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
                 ('07/2025', '2025-02-30', None, 'day', 'invalid'),
                 ('07/2025', '2025-07-11 wrong suffix', None, None, 'unrecognized'),
                 ('07/2025', '03/04/2025', None, 'day', 'ambiguous')]
        listing = '<main>Displaying 1 - 6 of 6'+''.join(
            f'<div class="views-row"><a href="/publications/research-{n}">Research {n}</a><small>Publication Date: {month}</small></div>'
            for n,(month,*_) in enumerate(cases))+'</main>'
        def fetch(url):
            if '?page=' in url: return listing
            raw = cases[int(url.rsplit('-',1)[1])][1]
            stamp = f'<div class="field--name-field-cecc-publication-date"><time datetime="{raw}"></time></div>' if raw else ''
            return '<h1>Research</h1><div class="field--name-body">'+'Scientific research. '*10+'</div>'+stamp
        before = None
        for expected in (len(cases), 0):
            c = self.module.NINDSNIHPublicationsCrawler(self.conn); c.delivery_mode = 'full'
            try:
                with patch.object(c,'_curl_get',side_effect=fetch):
                    self.assertEqual(c.crawl(),expected)
            finally:
                c._session.close()
            rows=self.conn.execute('SELECT seq_id,published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
            values=[tuple(row.values()) if isinstance(row,dict) else tuple(row) for row in rows]
            if before is not None: self.assertEqual(values,before)
            before=values
        self.assertEqual(len(values),len(cases))
        for row,(month,detail,value,precision,status) in zip(values,cases):
            with self.subTest(raw=detail or month):
                self.assertEqual(row[1:],(value,detail or month,precision,status))


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class NindsRawDatesPostgres(NindsRawDates):
    BACKEND='postgres'
