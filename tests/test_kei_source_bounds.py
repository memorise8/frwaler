"""KEI source listing contracts and native removal of silent traversal limits."""
import html,os,unittest
from types import SimpleNamespace
from urllib.parse import urlencode,parse_qs,urlsplit
from unittest.mock import Mock,patch
from crawler.kei_source import BASE,SCOPES,parse_listing,page_number
from tests.test_kei_document_identity import KeiIdentity

def fixture(scope='researchreport',page=1,total=20):
    path,mid,name=SCOPES[scope];kind='elibName' if path=='/elibList.es' else 'bid';key='c_id' if path=='/elibList.es' else 'list_no';last=max(1,(total+9)//10)
    def url(number,**kw):return BASE+path+'?'+urlencode(dict(mid=mid,**{kind:name},nPage=number,**kw))
    form=f'<form class="Search" action="{html.escape(url(page))}"><input name="mid" value="{mid}"><input name="{kind}" value="{name}"><input name="b_list" value="10"></form>'
    info=f'<div class="board_info">{form}<p class="page_info"><span class="total_page"><b>{total:,}건</b></span><span class="current_page"><strong>{page}</strong>/<b>{last}</b></span></p></div>'
    rows=[]
    for number in range((page-1)*10+1,min(page*10,total)+1):
        link=html.escape(url(page,act='view',**{key:str(number)}));title=f'Environmental research {number}'
        if scope=='board-0009':rows.append(f'<div class="lst_item"><a href="{link}" title="{title}"><div class="title"><strong>{title}</strong></div><div class="more"><li>작성일<span>2026-08-31</span></li></div></a></div>')
        else:rows.append(f'<ul><li class="num">{number}</li><li class="title"><a href="{link}" title="{title}">Truncated..</a></li><li class="date-02">2026-08-31</li><li class="file"><a href="/boardDownload.es?bid=0028&amp;list_no={number}&amp;seq=1">File</a></li></ul>')
    wrap='<div class="bstyle_list">' if scope=='board-0009' else '<div class="dbody" id="listView">'
    pager=f'<div class="board_pager"><a href="{html.escape(url(page))}">current</a><a href="{html.escape(url(min(page+1,last)))}">next</a></div>'
    return info+wrap+''.join(rows)+'</div>'+pager

class KeiLists(unittest.TestCase):
    def test_normal_title_attribute_line_breaks_are_not_corruption(self):
        raw=fixture(total=1).replace('title="Environmental research 1"','title="Environmental\nresearch\t1"')
        item=parse_listing(raw,'researchreport',1)['items'][0]
        self.assertNotIn('title_quality',item)
        self.assertEqual(item['source_key'],'researchreport:1')

    def test_source_null_title_is_flagged_without_losing_its_id_or_date(self):
        raw=fixture('board-0009',total=1).replace('Environmental research 1','Source\x00title')
        item=parse_listing(raw,'board-0009',1)['items'][0]
        self.assertEqual(item['source_key'],'board-0009:1')
        self.assertEqual(item['date_raw'],'2026-08-31')
        self.assertEqual(item['title'],'Source\ufffdtitle')
        self.assertEqual(item['title_quality']['state'],'requires_review')
        self.assertEqual(item['title_quality']['replacement_characters'],1)

    def test_press_nonempty_broken_attribute_cannot_truncate_visible_headline(self):
        raw=fixture('board-0009',total=1).replace('title="Environmental research 1"', 'title="KEI "기후-대기 통합관리의 공편익" 국제 워크숍 개최"').replace('<strong>Environmental research 1</strong>', '<strong>KEI "기후-대기 통합관리의 공편익" 국제 워크숍 개최</strong>')
        item=parse_listing(raw,'board-0009',1)['items'][0]
        self.assertEqual(item['title'],'KEI "기후-대기 통합관리의 공편익" 국제 워크숍 개최')
        self.assertEqual(item['source_key'],'board-0009:1')

    def test_catalogue_keeps_full_attribute_when_visible_title_is_abbreviated(self):
        item=parse_listing(fixture('researchreport',total=1),'researchreport',1)['items'][0]
        self.assertEqual(item['title'],'Environmental research 1')
        self.assertEqual(item['title_text'],'Truncated..')

    def test_press_whitespace_attribute_preserves_visible_quoted_title(self):
        raw=fixture('board-0009',total=1).replace('title="Environmental research 1"', 'title="  "GMO 작물 천덕꾸러기 될 지도""').replace('<strong>Environmental research 1</strong>', '<strong>  "GMO 작물 천덕꾸러기 될 지도"</strong>')
        item=parse_listing(raw,'board-0009',1)['items'][0]
        self.assertEqual(item['title'],'"GMO 작물 천덕꾸러기 될 지도"')
        self.assertEqual(item['source_key'],'board-0009:1')
        self.assertEqual(item['date_raw'],'2026-08-31')

    def test_press_both_titles_blank_remains_an_explicit_hold(self):
        raw=fixture('board-0009',total=1).replace('Environmental research 1','   ')
        with self.assertRaisesRegex(ValueError,'listing title missing'):
            parse_listing(raw,'board-0009',1)

    def test_forum_pager_without_catalogue_keeps_selected_source_catalogue(self):
        # Actual KEI forum links omit elibName; the server then serves research.
        raw=fixture('environmentalforum').replace('&amp;elibName=environmentalforum&amp;nPage=2', '&amp;nPage=2')
        data=parse_listing(raw,'environmentalforum',1)
        query=parse_qs(urlsplit(data['next_url']).query)
        self.assertEqual(query['elibName'],['environmentalforum'])
        self.assertEqual(query['nPage'],['2'])
        self.assertEqual(query['mid'],['a10102010000'])

    def test_explicit_wrong_catalogue_in_pager_is_never_repaired(self):
        raw=fixture('environmentalforum').replace('&amp;elibName=environmentalforum&amp;nPage=2', '&amp;elibName=researchreport&amp;nPage=2')
        with self.assertRaises(ValueError):parse_listing(raw,'environmentalforum',1)

    def test_all_eleven_routes_preserve_scope_source_id_title_date_and_next_url(self):
        self.assertEqual(len(SCOPES),11)
        for scope in SCOPES:
            data=parse_listing(fixture(scope),scope,1);self.assertEqual((data['total'],data['last_page'],len(data['items'])),(20,2,10));self.assertEqual(page_number(data['next_url'],scope),2)
            for i,r in enumerate(data['items'],1):self.assertEqual(r['source_key'],f'{scope}:{i}');self.assertEqual(r['title'],f'Environmental research {i}');self.assertEqual(r['date_raw'],'2026-08-31')
    def test_terminal_partial_page_has_exact_rows_and_no_successor(self):
        d=parse_listing(fixture(page=201,total=2001),'researchreport',201);self.assertEqual(len(d['items']),1);self.assertIsNone(d['next_url'])
    def test_valid_empty_scope_requires_counters_and_form(self):
        self.assertEqual(parse_listing(fixture(total=0),'researchreport',1)['items'],[])
        with self.assertRaises(ValueError):parse_listing('<html>No data</html>','researchreport',1)
    def test_missing_card_source_form_or_counter_is_not_empty_success(self):
        raw=fixture()
        for broken in [raw.replace('id="listView"','id="unknown"'),raw.replace('20건','21건'),raw.replace('value="10"','value="25"'),raw.replace('class="Search"','class="unknown"')]:
            with self.assertRaises(ValueError):parse_listing(broken,'researchreport',1)
    def test_repeated_id_wrong_scope_wrong_page_and_filtered_link_hold(self):
        raw=fixture()
        for broken in [raw.replace('c_id=2&amp;','c_id=1&amp;').replace('c_id=2"','c_id=1"'),raw.replace('a10101010000','a10102010000'),raw.replace('<strong>1</strong>','<strong>2</strong>'),raw.replace('act=view','keyWord=water&amp;act=view')]:
            with self.assertRaises(ValueError):parse_listing(broken,'researchreport',1)
    def test_missing_or_cross_host_next_link_is_rejected(self):
        raw=fixture()
        for broken in [raw.replace('nPage=2','nPage=1'),raw.replace('https://www.kei.re.kr/elibList.es','https://outside.invalid/elibList.es')]:
            with self.assertRaises(ValueError):parse_listing(broken,'researchreport',1)
    def test_foreign_and_duplicate_scope_queries_are_rejected(self):
        for u in [BASE+'/elibList.es?mid=a10101010000&mid=a10101010000',BASE+'/elibList.es?mid=a10101010000&nPage=2&unknown=1',BASE+'/elibList.es?mid=a10101010000&nPage=0']:
            with self.assertRaises(ValueError):page_number(u,'researchreport')

class KeiBounds(KeiIdentity):
    def test_damaged_detail_title_is_not_persisted_as_a_healthy_article(self):
        c=self.crawler();c.delivery_mode='backfill'
        c._fetch_detail=lambda *args,**kw:dict(title='Source\ufffdtitle',abstract='Source body. '*20)
        self.assertEqual(c.crawl(),0);self.assertEqual(self.count(),0)
        self.assertEqual(c.delivery_source_failure,'item_failed')

    def test_default_traversal_reaches_page_201_instead_of_silently_stopping_at_200(self):
        c=self.crawler();self.assertEqual(c._MAX_PAGES,0)
        one=c._fetch_list_page(1)[0];requests=[]
        def listing(page):
            requests.append(page);c._list_bounds=(201,201)
            return [dict(one,c_id=str(page),url=one['url'].replace('c_id=101','c_id='+str(page)))]
        c._fetch_list_page=listing;self.assertEqual(c.crawl(),201);self.assertEqual(requests,list(range(1,202)));self.assertEqual(self.count(),201)
        self.assertFalse(c.delivery_exhausted) # Remaining delivered routes not yet native.
    def test_real_list_parser_follows_published_next_link_and_retains_bounds(self):
        c=self.crawler();c._fetch_list_page=self.module.KeiReKrEliblistesCrawler._fetch_list_page.__get__(c)
        c._curl_get=Mock(side_effect=lambda url,**kw:fixture(page=int(parse_qs(urlsplit(url).query).get('nPage',['1'])[0]),total=11))
        self.assertEqual(c.crawl(),11);self.assertEqual(c._list_bounds,(11,2));self.assertEqual(c._curl_get.call_count,2)
        self.assertEqual(c.delivery_source_failure,'configuration');self.assertFalse(c.delivery_exhausted)
    def test_explicit_page_budget_reports_incomplete(self):
        c=self.crawler();c._MAX_PAGES=1;self.assertEqual(c.crawl(),4);self.assertEqual(c.delivery_source_failure,'invalid_response');self.assertFalse(c.delivery_exhausted)
    def test_wall_budget_reports_incomplete(self):
        c=self.crawler();c._TIMEOUT_SECS=0
        with patch.object(self.module.time,'time',side_effect=[0,1]):self.assertEqual(c.crawl(),0)
        self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_body_gap_is_reported_and_backfill_does_not_skip_it(self):
        c=self.crawler();c._fetch_detail=lambda *a,**kw:{};c.delivery_mode='backfill';self.assertEqual(c.crawl(),0);self.assertEqual(c.delivery_source_failure,'item_failed')
    def test_list_exception_stops_instead_of_skipping_to_later_pages(self):
        c=self.crawler();c._fetch_list_page=Mock(side_effect=ValueError('source mismatch'));self.assertEqual(c.crawl(),0);self.assertEqual(c._fetch_list_page.call_count,1);self.assertEqual(c.delivery_source_failure,'invalid_response')
    def test_http_access_denials_are_held_and_not_parsed_as_articles(self):
        for status,reason in [(401,'authentication_failed'),(403,'authentication_failed'),(429,'rate_limited')]:
            c=self.crawler()
            with patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(stdout=b'<html>Access denied</html>\n'+str(status).encode(),returncode=0)) as run:self.assertIsNone(c._curl_get(BASE));self.assertEqual(run.call_count,1)
            self.assertEqual(c.delivery_source_failure,reason)
    def test_http_error_body_never_becomes_successful_html(self):
        c=self.crawler()
        with patch.object(self.module.subprocess,'run',return_value=SimpleNamespace(stdout=b'<html>Error</html>\n500',returncode=0)) as run:self.assertIsNone(c._curl_get(BASE));self.assertEqual(run.call_count,3)
        self.assertEqual(c.delivery_source_failure,'fetch_failed')
    def test_detail_publication_raw_date_is_not_destroyed_before_common_normalization(self):
        c=self.crawler();c._curl_get=Mock(return_value='<div class="tstyle_view"><div class="right"><ul><li><span>발간일</span><em>2025</em></li></ul></div></div>')
        detail=self.module.KeiReKrEliblistesCrawler._fetch_detail(c,BASE,BASE);self.assertEqual(detail['published_date'],'2025')

@unittest.skipUnless(os.environ.get('TEST_PG_DSN'),'requires owned disposable PostgreSQL')
class KeiBoundsPostgres(KeiBounds):BACKEND='postgres'
