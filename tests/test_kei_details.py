"""KEI detail identity, date roles, body limits and attachment provenance."""
import html
import unittest
from urllib.parse import urlencode

from crawler.kei_details import parse_detail
from crawler.kei_source import BASE, SCOPES


def fixture(scope='board-0028', *, body='<p>실제 본문</p>', filename='report.pdf'):
    path, mid, name = SCOPES[scope]
    board = scope.startswith('board-')
    key, selector = ('list_no', 'bid') if board else ('c_id', 'elibName')
    item = dict(scope=scope, source_id='123', title='환경 연구 전체 제목', date_raw='2026-09-30',
                url=BASE+path+'?'+urlencode(dict(mid=mid, act='view', **{selector:name, key:'123'})))
    hidden = ''.join(f'<input name="{k}" value="{v}">' for k, v in
                     [('mid', mid), (selector, name), (key, '123')])
    if board:
        label = '작성일시' if scope == 'board-0009' else '발간일'
        content = (f'<div class="title">{item["title"]}</div><ul class="head">'
                   f'<li class="date"><strong>{label}</strong><span>2026-09-30 14:41</span></li>'
                   '<li class="write"><strong>작성자</strong><span>관리자</span></li></ul>'
                   f'<div class="tb_contents">{body}</div>')
        file_url = f'/boardDownload.es?bid={name}&list_no=123&seq=1'
    else:
        content = (f'<div class="report_title"><div class="right"><strong class="tit">{item["title"]}</strong>'
                   '<ul><li><span>발간일</span><em>2026-09-30</em></li>'
                   '<li><span>저자</span><em>저자 가</em></li>'
                   '<li><span>연구진</span><em>저자 나; 저자 다</em></li></ul></div></div>'
                   f'<div id="view1">목차: 방법, 결과</div><div id="view2">{body}</div>')
        file_url = 'https://library.kei.re.kr/pyxis-api/1/digital-files/7db99185-c0af-4f67-b468-122b89dcf708'
    files = f'<ul class="add_file_list"><li><a class="file-down" href="{html.escape(file_url)}">{filename}</a><span class="fileSize">1MB</span></li></ul>'
    return hidden+'<div class="tstyle_view">'+content+files+'</div>', item


class KeiDetails(unittest.TestCase):
    def test_catalogue_attribute_whitespace_is_preserved_but_compared_consistently(self):
        raw,item=fixture('researchreport')
        title=item['title'].replace(' ', '  ', 1)
        r=parse_detail(raw.replace(item['title'],title),dict(item,title=title))
        self.assertEqual(r['title'],item['title'])
        self.assertEqual(r['listing_title'],title)
        self.assertEqual(r['title_binding'],'source_title_whitespace_normalized')
        with self.assertRaises(ValueError):
            parse_detail(raw,dict(item,title=title+' 다른 제목'))

    def test_damaged_source_title_is_held_for_review(self):
        raw,item=fixture()
        for changed,wanted in [(raw.replace(item['title'],'Source\ufffdtitle'),dict(item,title='Source\ufffdtitle')),
                               (raw,dict(item,title_quality={'state':'requires_review'}))]:
            with self.assertRaisesRegex(ValueError,'title quality requires review'):
                parse_detail(changed,wanted)

    def test_all_eleven_scope_ids_and_explicit_date_roles(self):
        for scope in SCOPES:
            with self.subTest(scope=scope):
                raw, item = fixture(scope)
                result = parse_detail(raw, item)
                self.assertEqual(result['source_key'], scope+':123')
                self.assertEqual(result['title'], item['title'])
                self.assertEqual(result['abstract'], '실제 본문')
                self.assertEqual(result['source_listing_date_raw'], '2026-09-30')
                if scope == 'board-0009':
                    self.assertIsNone(result['published_date_raw'])
                    self.assertEqual(result['listed_date_raw'], '2026-09-30 14:41')
                else:
                    self.assertTrue(result['published_date_raw'].startswith('2026-09-30'))
                self.assertFalse(result['file_bytes_verified'])
                self.assertFalse(result['full_text_fidelity_verified'])

    def test_image_alt_and_file_name_never_become_body(self):
        raw, item = fixture(body='<img src="/upload/report.jpg" alt="긴 보고서 제목과 내용">')
        r = parse_detail(raw, item)
        self.assertEqual(r['abstract'], '')
        self.assertEqual(r['body_classification'], 'empty_text_with_images')
        self.assertEqual(r['body_images'][0]['alt'], '긴 보고서 제목과 내용')
        self.assertEqual(r['primary_PDF_candidate']['filename'], 'report.pdf')

    def test_hwp_is_preserved_without_being_called_a_PDF(self):
        raw, item = fixture('board-0009', filename='보도자료.hwp')
        r = parse_detail(raw, item)
        self.assertEqual(r['attachments'][0]['extension'], '.hwp')
        self.assertIsNone(r['primary_PDF_candidate'])

    def test_ellipsis_is_preserved_and_reported_without_inventing_full_body(self):
        raw, item = fixture(body='<p>원문 자체의 짧은 요약...</p>')
        r = parse_detail(raw, item)
        self.assertEqual(r['abstract'], '원문 자체의 짧은 요약...')
        self.assertTrue(r['body_ends_with_ellipsis'])

    def test_writer_is_separate_from_catalogue_authors_and_toc(self):
        raw, item = fixture()
        r = parse_detail(raw, item)
        self.assertEqual(r['writer'], '관리자')
        self.assertEqual(r['authors'], [])
        raw, item = fixture('researchreport')
        r = parse_detail(raw, item)
        self.assertEqual(r['authors'], ['저자 가', '저자 나', '저자 다'])
        self.assertEqual(r['contents'], '목차: 방법, 결과')
        self.assertNotIn('목차', r['abstract'])

    def test_full_detail_title_requires_exact_title_or_actual_listing_ellipsis(self):
        raw, item = fixture()
        for title in ['환경 연구..', '환경 연구…']:
            self.assertEqual(parse_detail(raw, dict(item, title=title))['title'], item['title'])
        for title in ['환경 연구', '다른 연구...', '...', '']:
            with self.subTest(title=title), self.assertRaises(ValueError):
                parse_detail(raw, dict(item, title=title))

    def test_response_form_must_match_requested_scope_and_document(self):
        raw, item = fixture()
        for changed in [raw.replace('name="list_no" value="123"', 'name="list_no" value="456"'),
                        raw.replace('name="bid" value="0028"', 'name="bid" value="0009"'),
                        raw.replace('name="mid" value="a10102020000"', 'name="mid" value="a10307020000"'),
                        raw.replace('name="list_no"', 'name="unknown"'),
                        raw+'<input name="list_no" value="456">']:
            with self.assertRaises(ValueError):
                parse_detail(changed, item)

    def test_requested_identity_cannot_be_changed_by_url_or_scope(self):
        raw, item = fixture()
        for changed in [dict(item, source_id='456'), dict(item, source_id='1:2'),
                        dict(item, scope='unknown'), dict(item, url=item['url'].replace('act=view', 'act=list')),
                        dict(item, url=item['url']+'&list_no=456')]:
            with self.assertRaises(ValueError):
                parse_detail(raw, changed)

    def test_wrong_board_download_id_or_ambiguous_query_is_rejected(self):
        raw, item = fixture()
        for changed in [raw.replace('list_no=123', 'list_no=456'),
                        raw.replace('bid=0028', 'bid=0009'),
                        raw.replace('seq=1', 'seq=0'), raw.replace('seq=1', 'seq=1&amp;seq=2'),
                        raw.replace('/boardDownload.es?', 'https://outside.invalid/boardDownload.es?')]:
            with self.assertRaises(ValueError):
                parse_detail(changed, item)

    def test_catalogue_download_keeps_only_source_library_UUID_destination(self):
        raw, item = fixture('environmentalforum')
        for changed in [raw.replace('library.kei.re.kr', 'outside.invalid'),
                        raw.replace('7db99185-c0af-4f67-b468-122b89dcf708', 'not-a-UUID'),
                        raw.replace('https://library', 'http://library')]:
            with self.assertRaises(ValueError):
                parse_detail(changed, item)

    def test_explicit_empty_body_differs_from_missing_source_structure(self):
        raw, item = fixture(body='')
        self.assertEqual(parse_detail(raw, item)['body_classification'], 'empty_text')
        for changed in [raw.replace('class="tb_contents"', 'class="unknown"'),
                        raw.replace('class="tstyle_view"', 'class="unknown"')]:
            with self.assertRaises(ValueError):
                parse_detail(changed, item)

    def test_unreviewed_or_ambiguous_date_role_is_held(self):
        raw, item = fixture()
        for changed in [raw.replace('발간일', '알 수 없는 날짜'),
                        raw.replace('</ul>', '<li><strong>발간일</strong><span>2025-01-01</span></li></ul>', 1)]:
            with self.assertRaises(ValueError):
                parse_detail(changed, item)

    def test_scripts_and_navigation_text_are_not_article_text(self):
        raw, item = fixture(body='<p>본문</p><script>tracking</script><style>css</style><nav>메뉴</nav>')
        self.assertEqual(parse_detail(raw, item)['abstract'], '본문')


if __name__ == '__main__':
    unittest.main()
