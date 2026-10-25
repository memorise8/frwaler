"""S-open-g5: kmcc-go-kr-userdo enumeration split (crawl() unchanged)."""
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'kmcc-go-kr-userdo'
BASE = 'https://www.kmcc.go.kr'
LONG = '방송미디어통신위원회는 방송 공정성 강화를 위한 제도 개선안을 의결하고 관계 기관과 협의를 거쳐 내년 상반기부터 단계적으로 시행할 예정이라고 오늘 밝혔다.'


def row(num, seq, title):
    return (f'<tr><td>{num}</td><td><a href="/user.do;jsessionid=AB12?mode=view&amp;boardSeq={seq}">{title}</a></td>'
            f'<td>방송정책과</td><td>제1유형</td><td><a href="/download.do?fileSeq={seq}1"><img alt="자료{seq}.hwp"></a></td>'
            f'<td>2026-10-{num:02d}</td></tr>')


LIST = ('<html><body><table class="table_style01"><tbody>' + row(12, 7001, '제도 개선안 의결')
        + '<tr><td>공지</td><td>x</td><td></td><td></td><td></td><td></td></tr>'
        + row(11, 7000, '협의 결과') + row(10, 6999, '') + '</tbody></table></body></html>')
EMPTY = '<html><body><table class="table_style01"><tbody><tr><td colspan="6">게시물이 없습니다</td></tr></tbody></table></body></html>'


def detail(title, content, date, files=''):
    return (f'<html><body><table summary="제목, 담당부서, 작성자"><tr><td class="table_title">{title}</td></tr>'
            f'<tr><th>담당부서</th><td>방송정책과</td></tr><tr><th>작성자</th><td>홍길동</td></tr>'
            f'<tr><th>등록일</th><td>{date}</td></tr><tr><th>첨부파일</th><td>{files}</td></tr>'
            f'<tr><td class="table_con">{content}</td></tr></table></body></html>')


def durl(seq):
    return f'{BASE}/user.do?mode=view&page=A05030000&dc=K05030000&boardId=1113&boardSeq={seq}'


DETAILS = {
    durl(7001): detail('제도 개선안 의결', LONG, '2026-10-12', '<a href="/download.do?fileSeq=1"><span>개선안.pdf</span></a><a href="/download.do?fileSeq=1">다운로드</a>'),
    durl(7000): detail('협의 결과', LONG, '2026-10-11'),
    durl(6999): detail('', '', ''),
}


def route(url, *a, **k):
    if url in DETAILS:
        return DETAILS[url]
    return LIST if 'cp=1&' in url else EMPTY


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        expected, _ = native_papers(SITE, route, method_patches=['_curl'])
        parsed, papers, _ = split_run(SITE, LIST, DETAILS, page=1)
        self.assertEqual(['7001', '7000', '6999'], [i.key for i in parsed.items])
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        p = papers[0]
        self.assertEqual(('7001', '12', '2026-10-12'), (p['external_id'], p['post_number'], p['published_date']))
        self.assertEqual(BASE + '/download.do?fileSeq=1', p['pdf_url'])
        self.assertEqual(durl(7001), p['url'])

    def test_empty_page_and_wrong_shape(self):
        page = parse_page(SITE, EMPTY, page=50)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items('<html><body><form id="login"></form></body></html>')


if __name__ == '__main__':
    unittest.main()
