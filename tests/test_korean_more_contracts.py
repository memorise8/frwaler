"""Native government HTML paths; real persistence, controlled source responses."""
import os
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from tests.test_coupled_site_counts import CoupledContract

DATES = [('2025', None, 'year', 'partial'), ('2025.07', None, 'month', 'partial'),
         ('2025.02.30', None, 'day', 'invalid'),
         ('2025-07-11T12:34:56Z', '2025-07-11', 'day', 'normalized'),
         ('2025-07-11 wrong suffix', None, None, 'unrecognized'),
         ('03/04/2025', None, 'day', 'ambiguous'),
         ('20250711', '2025-07-11', 'day', 'normalized'),
         ('2025년 7월 11일', '2025-07-11', 'day', 'normalized')]


class GovernmentContract(CoupledContract):
    def test_failed_listing_does_not_look_like_empty_success(self):
        c=self.crawler()
        owner=self.module if getattr(self,'LIST_MODULE',False) else c
        with patch.object(owner,self.LIST_METHOD,return_value=None):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'fetch_failed')
        self.assertFalse(c.delivery_exhausted)

    def test_native_identity_uses_source_key_and_keeps_sequence_ids(self):
        self.assertEqual(self.crawler().crawl(), 8)
        before = self.identities()
        prefix = 'DPIM_' if self.SITE == 'mnd-go-kr-user' else ''
        self.assertEqual(set(before), {prefix+str(n) for n in range(1001,1009)})
        self.display_shift = 100
        self.assertEqual(self.crawler().crawl(), 0)
        self.assertEqual(self.identities(), before)

    def test_date_precision_survives_native_detail_parser(self):
        self.assertEqual(self.crawler().crawl(), 8)
        rows = self.conn.execute('SELECT published_date,published_date_raw,published_date_precision,published_date_status FROM documents ORDER BY seq_id').fetchall()
        for row, (raw, value, precision, status) in zip(rows, DATES):
            values = tuple(row.values()) if isinstance(row, dict) else tuple(row)
            with self.subTest(raw=raw): self.assertEqual(values, (value,raw,precision,status))
        listed=self.conn.execute('SELECT listed_date,listed_date_raw FROM documents').fetchall()
        for row in listed:
            values=tuple(row.values()) if isinstance(row,dict) else tuple(row)
            self.assertEqual(values,('2025-07-11','2025.07.11'))


class MndCounts(GovernmentContract, unittest.TestCase):
    SITE='mnd-go-kr-user'; LIST_METHOD='_curl'; LIST_MODULE=True
    def crawler(self):
        c = self.configured(self.module.MndGoKrUserCrawler(self.conn))
        def fetch(method,url,**kwargs):
            if method == 'GET':
                n = int(url.split('DPIM_')[1].split('/')[0])-1000
                return f'<input id="artclViewTitle" value="자료 {n}"><ul class="detail"><li><span>작성일</span> {DATES[n-1][0]}</li></ul><div class="txt">'+('연구 결과와 정책 제안입니다. '*20)+'</div>'
            page = int(parse_qs(kwargs['data'])['page'][0])
            return '<table><tbody>'+''.join(f'<tr><td>{n-1000}</td><td><a href="/bbs/DPIM_{n}/artclView.do"><strong>자료 {n}</strong></a></td><td>2025.07.11</td></tr>' for n in {1:range(1001,1006),2:range(1006,1009)}.get(page,[]))+'</tbody></table>'
        self.fetch = self.enterContext(patch.object(self.module,'_curl',side_effect=fetch))
        return c

    def test_failed_detail_is_reported(self):
        c=self.crawler(); original=self.fetch.side_effect
        self.fetch.side_effect=lambda method,url,**kw:None if method=='GET' else original(method,url,**kw)
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_parser_failure_is_not_a_valid_empty_list(self):
        c=self.crawler()
        with patch.object(self.module,'_make_soup',side_effect=ValueError('controlled parser error')):
            self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response')


class MolitCounts(GovernmentContract, unittest.TestCase):
    SITE='molit-go-kr-usr'; LIST_METHOD='_request'
    def crawler(self):
        c = self.configured(self.module.MolitGoKrUsrCrawler(self.conn))
        def listing(url,params,**kw):
            response=Mock(); response.content=('<table class="bd_tbl"><tbody>'+''.join(f'<tr><td class="bd_num">{n-1000+getattr(self,"display_shift",0)}</td><td class="bd_title"><a href="dtl.jsp?id={n}">자료 {n}</a></td><td class="bd_date">2025.07.11</td></tr>' for n in {1:range(1001,1006),2:range(1006,1009)}.get(int(params['lcmspage']),[]))+'</tbody></table>').encode();return response
        def detail(session,url,**kw):
            n=int(parse_qs(urlsplit(url).query)['id'][0])-1000
            response=Mock();response.content=(f'<div class="bd_view"><h4>자료 {n}</h4><ul class="bd_view_ul_info"><li><strong>등록일</strong><span>{DATES[n-1][0]}</span></li></ul><ul class="bd_view_ul_lst"><li>'+('연구 결과와 정책 제안입니다. '*20)+'</li></ul></div>').encode();return response
        c._request=listing
        self.detail=self.enterContext(patch.object(self.module,'_fetch_with_retry',side_effect=detail))
        return c

    def test_failed_detail_is_reported(self):
        c=self.crawler();self.detail.side_effect=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')


class GangwonCounts(GovernmentContract, unittest.TestCase):
    SITE='state-gwd-go-kr-portal'; LIST_METHOD='_curl_post'
    def crawler(self):
        c=self.configured(self.module.StateGwdGoKrPortalCrawler(self.conn))
        def listing(url,form,**kw):
            return '<table>'+''.join(f'<tr><td>{n-1000+getattr(self,"display_shift",0)}</td><td><a href="/portal/briefing/pressRelease/newpressRelease?seq={n}">'+('자료와 정책 분석 '*10)+f'{n}</a></td><td class="skinTb-date">2025.07.11</td></tr>' for n in {1:range(1001,1006),2:range(1006,1009)}.get(int(form['pageIndex']),[]))+'</table>'
        def detail(url,**kw):
            n=int(parse_qs(urlsplit(url).query)['seq'][0])-1000
            return f'<main id="content-bx"><div class="skinTb-date">{DATES[n-1][0]}</div><div class="skinTb-name">연구 부서</div></main>'
        c._curl_post=listing;c._curl_get=detail
        return c

    def test_failed_detail_is_reported(self):
        c=self.crawler();c._curl_get=lambda *a,**k:None
        self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_failed_hwp_fetch_is_reported_even_when_title_fallback_is_long(self):
        c=self.crawler(); original=c._fetch_detail
        c._fetch_detail=lambda seq:dict(original(seq),attachments=[{'url':'https://fixture.invalid/file.hwp','filename':'file.hwp'}])
        c._download_bytes=Mock(return_value=None)
        self.assertEqual(c.crawl(),8)
        self.assertEqual(c.delivery_source_failure,'item_failed')
        self.assertFalse(c.delivery_exhausted)


for cls in (MndCounts,MolitCounts,GangwonCounts):
    pg=type(cls.__name__+'Postgres',(cls,),{'BACKEND':'postgres'})
    globals()[pg.__name__]=unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')(pg)
del cls,pg
