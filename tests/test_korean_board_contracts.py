"""Native Korean board parsers with actual SQLite/PostgreSQL persistence."""
import os
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from tests.test_coupled_site_counts import CoupledContract

DATES = [('2025', None, 'year', 'partial'), ('2025.07', None, 'month', 'partial'),
         ('2025.02.30', None, 'day', 'invalid'),
         ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
         ('2025-07-11 wrong suffix', None, None, 'unrecognized'),
         ('03/04/2025', None, 'day', 'ambiguous'),
         ('2025.07.11.', '2025-07-11', 'day', 'normalized'),
         ('2025년 7월 11일', '2025-07-11', 'day', 'normalized')]


class BoardContract(CoupledContract):
    LIST_MODULE = True

    def test_source_dates_preserve_precision_and_original_text(self):
        self.assertEqual(self.crawler().crawl(), 8)
        rows = self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row, (raw, normalized, precision, status) in zip(rows, DATES):
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            with self.subTest(raw=raw):
                self.assertEqual(values, (normalized, raw, precision, status))

    def test_native_record_ids_survive_repeated_fetch(self):
        self.assertEqual(self.crawler().crawl(), 8)
        before = self.identities()
        self.assertEqual(set(before), {str(n) for n in range(1001, 1009)})
        self.display_shift = 100
        self.assertEqual(self.crawler().crawl(), 0)
        self.assertEqual(self.identities(), before)

    def test_failed_listing_reports_failure(self):
        c = self.crawler()
        with patch.object(self.module, self.LIST_METHOD, return_value=None):
            self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'fetch_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_failed_detail_is_not_a_content_exclusion(self):
        c = self.crawler(); original = self.detail.side_effect
        self.detail.side_effect = lambda url, *args, **kwargs: None if self.is_detail(url) else original(url, *args, **kwargs)
        self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'item_failed')


class KisdiCounts(BoardContract, unittest.TestCase):
    SITE = 'kisdi-re-kr-bbs'; LIST_METHOD = '_curl_get'
    is_detail = staticmethod(lambda url: '/view.do' in url)

    def crawler(self):
        c = self.configured(self.module.KisdiRekrBbsCrawler(self.conn))
        def fetch(url, params=None):
            if self.is_detail(url):
                n = int(parse_qs(urlsplit(url).query)['bbsSn'][0]) - 1000
                return f'<h4>연구 자료 {n}</h4><div><strong>등록일</strong> {DATES[n-1][0]}</div><div class="content">'+('연구 결과와 정책 제안입니다. '*20)+'</div>'
            return '<main>'+''.join(f'<a onclick="goView(\'{n}\')">자료</a>' for n in {1:range(1001,1006),2:range(1006,1009)}.get(int(params['pageIndex']), []))+'</main>'
        self.detail = self.enterContext(patch.object(self.module, '_curl_get', side_effect=fetch))
        return c

    def test_failed_listing_parse_does_not_skip_to_later_pages(self):
        c = self.crawler()
        with patch.object(self.module, '_bs', side_effect=ValueError('controlled parse failure')):
            self.assertEqual(c.crawl(), 0)
        self.assertEqual(c.delivery_source_failure, 'invalid_response')
        self.assertEqual(self.detail.call_count, 1)


class KotiCounts(BoardContract, unittest.TestCase):
    SITE = 'koti-re-kr-user'; LIST_METHOD = '_curl_get'
    is_detail = staticmethod(lambda url: 'ReprtView.do' in url)

    def crawler(self):
        c = self.configured(self.module.KotiReKrUserCrawler(self.conn))
        def fetch(url, **kwargs):
            q = parse_qs(urlsplit(url).query)
            if self.is_detail(url):
                n = int(q['bbs_no'][0]) - 1000
                return f'<strong class="title">연구 자료 {n}</strong><ul><li><em>발간일</em><p>{DATES[n-1][0]}</p></li></ul><dl><dt>주요 내용</dt><dd><div class="editor">'+('연구 결과와 정책 제안입니다. '*20)+'</div></dd></dl>'
            return '<main>'+''.join(f'<a href="/user/bbs/bassRsrchReprtView.do?bbs_no={n}">자료</a>' for n in {1:range(1001,1006),2:range(1006,1009)}.get(int(q['pg'][0]), []))+'</main>'
        self.detail = self.enterContext(patch.object(self.module, '_curl_get', side_effect=fetch))
        return c


class KcgCounts(BoardContract, unittest.TestCase):
    SITE = 'kcg-go-kr-kcg'; LIST_METHOD = '_curl_post'
    is_detail = staticmethod(lambda url: 'selectNttInfo.do' in url)

    def crawler(self):
        c = self.configured(self.module.KcgGoKrKcgCrawler(self.conn))
        def listing(url, data):
            return '<table><tbody>'+''.join(f'<tr><td class="BD_tm_none">{n-1000+getattr(self,"display_shift",0)}</td><td class="ta_l"><a href="/kcg/na/ntt/selectNttInfo.do?nttSn={n}">연구 자료 {n}</a></td><td>정책 부서</td><td>2025.07.11</td></tr>' for n in {1:range(1001,1006),2:range(1006,1009)}.get(int(data['currPage']), []))+'</tbody></table>'
        def detail(url):
            n = int(parse_qs(urlsplit(url).query)['nttSn'][0]) - 1000
            return f'<table><tr><th class="title">연구 자료 {n}</th></tr><tr><th>등록일</th><td>{DATES[n-1][0]}</td></tr><tr><td style="word-break:break-all">'+('연구 결과와 정책 제안입니다. '*20)+'</td></tr></table>'
        self.enterContext(patch.object(self.module, '_curl_post', side_effect=listing))
        self.detail = self.enterContext(patch.object(self.module, '_curl_get', side_effect=detail))
        return c


for cls in (KisdiCounts, KotiCounts, KcgCounts):
    pg = type(cls.__name__+'Postgres', (cls,), {'BACKEND': 'postgres'})
    globals()[pg.__name__] = unittest.skipUnless(os.environ.get('TEST_PG_DSN'), 'requires owned disposable PostgreSQL')(pg)
del cls, pg
