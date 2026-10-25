"""S-open-g5: nec-go-kr-site enumeration split (crawl() unchanged).

crawl() reads the abstract from the row's PDF (curl + pdftotext). The plan requests that exact PDF as
the second step (url_template from the row's own link parts) and the framework extracts the text;
here both sides get the same text from a fake pdftotext."""
import unittest
from unittest.mock import patch

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load
from tests.split_open_g5_helpers import NoIO, comparable, native_papers, parse_page, split_run

SITE = 'nec-go-kr-site'
BASE = 'https://www.nec.go.kr'
TEXT = '중앙선거관리위원회는 공정한 선거 관리를 위해 위법행위 예방 활동을 강화한다고 밝혔다. 자세한 내용은 붙임 자료와 같다.'


def pdf(bc, name):
    return f'{BASE}/common/board/Download.do?bcIdx={bc}&cbIdx=1090&streFileNm={name}'


def li(num, bc, title, date, fname=None, href=None):
    files = (f'<span class="fileDown"><a href="{href}" title="{fname} 파일다운로드">pdf</a></span>' if href else '')
    return (f'<li><span class="num">{num}</span><a class="btn_bbsDetail" data-info1="{bc}" '
            f'href="/site/nec/ex/bbs/View.do?cbIdx=1090&amp;bcIdx={bc}">{title}</a>'
            f'<span class="date">{date}</span>{files}</li>')


LIST = ('<html><body><div class="tableList"><ul>'
        + li(30, 501, '위법행위 예방 활동 강화', '2026-10-20', '예방.pdf', '/common/board/Download.do?bcIdx=501&amp;cbIdx=1090&amp;streFileNm=a1.pdf')
        + li(29, 500, '공정 선거 캠페인', '2026-10-19', '캠페인.pdf', '/common/board/Download.do?bcIdx=500&amp;cbIdx=1090&amp;streFileNm=b2.pdf')
        + li(28, 499, '첨부 없음', '2026-10-18')
        + li(27, 498, '짧은 본문', '2026-10-17', '짧음.pdf', '/common/board/Download.do?bcIdx=498&amp;cbIdx=1090&amp;streFileNm=c3.pdf')
        + '</ul></div></body></html>')
EMPTY = '<html><body><div class="tableList"><ul><li class="nodata">등록된 게시물이 없습니다.</li></ul></div></body></html>'


def detail(date):
    return (f'<html><body><ul class="viewDetail"><li>등록일 : {date}</li></ul>'
            '<div class="satisfaction">담당부서: 공보과 / 전화</div></body></html>')


def durl(bc):
    return f'{BASE}/site/nec/ex/bbs/View.do?cbIdx=1090&bcIdx={bc}'


PDF_TEXT = {pdf(501, 'a1.pdf'): '  ' + TEXT + '\n', pdf(500, 'b2.pdf'): TEXT + ' 둘째.', pdf(498, 'c3.pdf'): '짧다'}
DETAILS = {durl(501): detail('2026-10-21'), durl(500): detail('2026-10-19'), durl(499): detail('2026-10-18'),
           durl(498): detail('2026-10-17')}
DETAILS.update({u: ('%PDF ' + u).encode() for u in PDF_TEXT})


def route(url, *a, **k):
    if url in DETAILS:
        return DETAILS[url]
    return LIST if url.endswith('pageIndex=1') else EMPTY


def fake_extract(body, spec):
    text = body.decode()
    return PDF_TEXT[text[5:]] if text.startswith('%PDF ') else None  # pdftotext fails on HTML


class Split(NoIO, unittest.TestCase):
    def test_split_matches_crawl(self):
        # crawl()'s _pdf_to_text(pdf_url) gets the same (stripped) text the framework extracts.
        expected, _ = native_papers(SITE, lambda url, *a, **k: PDF_TEXT[url].strip() if url in PDF_TEXT else route(url),
                                    module_patches=['_curl_get', '_pdf_to_text'])
        with patch('crawler.enumeration.pdftext.extract', fake_extract), \
                patch('crawler.enumeration.pdftext.available', lambda: True):
            parsed, papers, calls = split_run(SITE, LIST, DETAILS, page=1)
        self.assertEqual(['501', '500', '499', '498'], [i.key for i in parsed.items])
        self.assertEqual(2, len(expected))
        self.assertEqual([comparable(p) for p in expected], [comparable(p) for p in papers])
        p = papers[0]
        self.assertEqual(('501', '2026-10-21', '위법행위 예방 활동 강화'), (p['external_id'], p['published_date'], p['title']))
        self.assertEqual(pdf(501, 'a1.pdf'), p['pdf_url'])
        self.assertEqual(durl(501), p['url'])
        self.assertIn(pdf(501, 'a1.pdf'), calls)

    def test_empty_page_and_wrong_shape(self):
        with patch('crawler.enumeration.pdftext.available', lambda: True):
            page = parse_page(SITE, EMPTY, page=500)
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        c, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            c._enumeration_items('<html><body>접속 대기</body></html>')


if __name__ == '__main__':
    unittest.main()
