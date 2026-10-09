"""GOV.UK native search/detail path with actual SQLite/PostgreSQL persistence."""
import json
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs,urlsplit

from tests.test_coupled_site_counts import CoupledContract

DATES=[('2025',None,'year','partial'),('2025-07',None,'month','partial'),
       ('2025-02-30',None,'day','invalid'),('2025-07-11T12:34:56Z','2025-07-11','day','normalized'),
       ('2025-07-11 wrong suffix',None,None,'unrecognized'),('03/04/2025',None,'day','ambiguous'),
       ('2025-07-11','2025-07-11','day','normalized'),('',None,None,'missing')]


class GovUkCounts(CoupledContract,unittest.TestCase):
    SITE='gov-uk-search';LIST_METHOD='_curl_get';LIST_MODULE=True

    def crawler(self):
        c=self.configured(self.module.GovUKSearchCrawler(self.conn))
        def fetch(url,**kwargs):
            if '/api/content/' in url:
                n=int(url.rsplit('-',1)[1]);raw=DATES[n-1][0]
                return json.dumps({'content_id':str(n),'title':f'Research {n}','base_path':f'/government/publications/research-{n}',
                                   'description':'Scientific research. '*10,'first_published_at':raw,'public_updated_at':raw})
            page=int(parse_qs(urlsplit(url).query)['page'][0])
            return json.dumps({'search_results':''.join(f'<a href="/government/publications/research-{n}">Research</a>' for n in {1:range(1,6),2:range(6,9)}.get(page,[])),
                               'next_and_prev_links':'<div class="govuk-pagination__next"></div>' if page==1 else ''})
        self.fetch=self.enterContext(patch.object(self.module,'_curl_get',side_effect=fetch))
        return c

    def test_native_date_fields_retain_precision_and_invalid_originals(self):
        self.assertEqual(self.crawler().crawl(),8)
        rows=self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status,listed_date,listed_date_raw,listed_date_precision,listed_date_status FROM documents ORDER BY seq_id').fetchall()
        for row,(raw,value,precision,status) in zip(rows,DATES):
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            with self.subTest(raw=raw):
                self.assertEqual(values,(value,raw,precision,status,value,raw or None,precision,status))

    def test_failed_detail_is_reported_instead_of_empty_success(self):
        c=self.crawler();fetch=self.fetch.side_effect
        self.fetch.side_effect=lambda url,**kwargs:None if '/api/content/' in url else fetch(url,**kwargs)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertFalse(c.delivery_exhausted)


@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class GovUkCountsPostgres(GovUkCounts):
    BACKEND='postgres'
