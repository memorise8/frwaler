"""S-open-g5: gyeongnam-go-kr-board enumeration split (crawl() unchanged)."""
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'gyeongnam-go-kr-board'
BASE = 'https://www.gyeongnam.go.kr'
LONG = '경상남도는 도민의 안전을 위해 새로운 재난 대응 체계를 마련하고 시군과 함께 합동 점검을 실시한다고 밝혔다.'


def row(num, sid, title, date, dept='안전정책과'):
    return (f'<tr><td class="num">{num}</td><td class="title"><a href="/board/view.gyeong?boardId=BBS_0000060&amp;dataSid={sid}">{title}</a></td>'
            f'<td>파일</td><td class="date">{date}</td><td>{dept}</td></tr>')


LIST = ('<html><body><table class="basicList"><tbody>' + row(3, 901, '재난 대응 체계 마련', '26.10.20')
        + '<tr><td class="num">공지</td><td class="title">링크 없음</td></tr>'
        + row(2, 900, '합동 점검 실시', '26.10.19') + row(1, 899, '짧은 글', '26.10.18') + '</tbody></table></body></html>')
EMPTY = '<html><body><table class="basicList"><tbody><tr><td colspan="5">등록된 게시물이 없습니다.</td></tr></tbody></table></body></html>'


def detail(text, date, files=''):
    return (f'<html><body><ul class="titleField"><li>등록일 {date}</li></ul><ul class="conField">'
            f'<li><span>구분</span><p>보도자료</p></li><li><span>제공부서</span><p>안전정책과</p></li>'
            f'<li><span>첨부파일</span><p>{files}</p></li></ul><div class="conText">{text}</div></body></html>')


def durl(sid):
    return f'{BASE}/board/view.gyeong?boardId=BBS_0000060&dataSid={sid}'


PAGES = {
    '901': detail(LONG, '26.10.21', '<a class="file" href="/upload/a.pdf">보도자료.pdf (120 kb)</a>'),
    '900': detail(LONG + ' 추가.', '26.10.19'),
    '899': detail('짧다', '26.10.18'),
}


def route(url, *a, **k):
    if 'view.gyeong' in url:
        sid = url.rsplit('dataSid=', 1)[1]
        return PAGES[sid]
    return LIST if 'pageNo=1' in url else EMPTY


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        expected, calls = native_papers(SITE, route, method_patches=['_curl'])
        parsed, papers, _ = split_run(SITE, LIST, {durl(s): b for s, b in PAGES.items()}, page=1)
        self.assertEqual(['901', '900', '899'], [i.key for i in parsed.items])
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        p = papers[0]
        self.assertEqual(('901', '3', '2026-10-21'), (p['external_id'], p['post_number'], p['published_date']))
        self.assertEqual(BASE + '/upload/a.pdf', p['pdf_url'])
        self.assertEqual(durl('901'), p['url'])

    def test_empty_page_and_wrong_shape(self):
        page = parse_page(SITE, EMPTY, page=99)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items('<html><body>점검 중</body></html>')


if __name__ == '__main__':
    unittest.main()
