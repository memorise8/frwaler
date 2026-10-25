"""S-open-g5: pps-go-kr-kor enumeration split (crawl() unchanged)."""
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'pps-go-kr-kor'
BASE = 'https://www.pps.go.kr'
BODY = '조달청은 2026년 3분기 공공조달 통계를 공개했다. 총 조달 실적은 전년 대비 증가했다.'


def row(num, sn, title, date):
    return (f'<tr><td class="listNum">{num}</td><td class="subject"><a href="#" onclick="goView(\'{sn}\'); return false;">{title}</a></td>'
            f'<td class="normal">통계담당</td><td class="normal">{date}</td></tr>')


LIST = ('<html><body><table class="board_list_table"><tbody>' + row(5, 'B0105', '3분기 조달 통계', '2026-10-01')
        + '<tr><td class="listNum">공지</td><td>링크 없음</td></tr>'
        + row(4, 'B0104', '2분기 조달 통계', '2026-07-01') + '</tbody></table></body></html>').encode('utf-8')
EMPTY = '<html><body><table class="board_list_table"><tbody><tr><td colspan="4">자료가 없습니다.</td></tr></tbody></table></body></html>'.encode()


def detail(title, body, date, file=''):
    return (f'<html><body><strong class="title">{title}</strong><div class="title_sp"><span>작성자 : 홍길동</span>'
            f'<span>등록일 : {date}</span><span>담당부서 : 정보관리과</span><span>전화번호 : 070-0000-0000</span></div>'
            f'<div id="brdContent">{body}</div>{file}</body></html>').encode('cp949')


def durl(sn):
    return f'{BASE}/kor/bbs/view.do?key=00664&bbsSn={sn}'


DETAILS = {
    durl('B0105'): detail('3분기 조달 통계', BODY, '2026-10-02', '<a href="/common/fileDown.do;jsessionid=X1?fileSn=9">통계(pdf)</a>'),
    durl('B0104'): detail('', BODY, ''),
}


def get(url, *a, **k):
    return DETAILS[url]


def post(page, *a, **k):
    return LIST if page == 1 else EMPTY


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        # crawl(): _curl_post_list(page) for the listing, _curl_get(url) for details.
        expected, _ = native_papers(SITE, lambda u, *a, **k: get(u) if isinstance(u, str) else post(u),
                                    module_patches=['_curl_get', '_curl_post_list'])
        parsed, papers, _ = split_run(SITE, LIST, DETAILS, page=1)
        self.assertEqual(['B0105', 'B0104'], [i.key for i in parsed.items])
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        p = papers[0]
        self.assertEqual(('B0105', '2026-10-02', '3분기 조달 통계'), (p['external_id'], p['published_date'], p['title']))
        self.assertEqual(BASE + '/common/fileDown.do?fileSn=9', p['pdf_url'])
        self.assertEqual('통계.pdf', p['original_filename'])
        self.assertEqual(('2분기 조달 통계', '2026-07-01'), (papers[1]['title'], papers[1]['published_date']))

    def test_empty_page_and_wrong_shape(self):
        page = parse_page(SITE, EMPTY, page=40)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items(b'<html><body>login</body></html>')
        with self.assertRaises(PermanentFailure):
            c._enumeration_items('text')


if __name__ == '__main__':
    unittest.main()
