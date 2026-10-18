"""ansto-gov-au-search: listing / detail fixes shared by crawl() and the enumeration methods.

Fixtures (tests/fixtures/ansto_search) are excerpts of live responses read on 2026-10-18:
the Views AJAX page 1 (query=pdf, 20 results: 8 HTML teasers + 12 direct PDF media), four
detail pages (description meta, first JSON-LD block, currentPath, by-line, first body
paragraph) and the first 1500 characters of `pdftotext -enc UTF-8` of /media/1137.
expected_before_fix.json holds what the collector produced before the fix (81ba0f7).
"""
import contextlib
import copy
import io
import json
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.sites import CRAWLERS

SITE = 'ansto-gov-au-search'
FX = Path(__file__).parent / 'fixtures' / 'ansto_search'
BASE = 'https://www.ansto.gov.au'
MEDIA_IDS = ['7360', '1137', '1138', '1139', '1410', '1000', '1004', '1010', '1114', '1115', '1121', '283']
# Position of each result on the live page 1 (H = HTML teaser, M = direct PDF media).
LAYOUT = 'HHHHHMHHHMMMMMMMMMMM'


def text(name):
    return (FX / name).read_text(encoding='utf-8')


class AnstoSearchFix(unittest.TestCase):
    def setUp(self):
        self.c = CRAWLERS[SITE](None, delay=0)
        self.addCleanup(self.c._session.close)
        self.g = self.c.crawl.__globals__
        self.expected = json.loads(text('expected_before_fix.json'))
        self.page1 = text('views_ajax_page1.json')
        self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.enterContext(patch('time.sleep'))

    # (1) results caught before the fix are unchanged ------------------------------------
    def test_previous_rows_unchanged_and_in_order(self):
        rows = self.g['_parse_search_response'](self.page1, 1)
        old = [r for r in rows if not self.g['_is_media_url'](r['url'])]
        self.assertEqual(self.expected['listing_page1_rows'], old)
        self.assertEqual(json.dumps(self.expected['listing_page1_rows'], ensure_ascii=False),
                         json.dumps(old, ensure_ascii=False))
        self.assertEqual(8, len(old))

    def test_previous_papers_byte_identical_except_filled_date(self):
        for name, case in self.expected['papers'].items():
            before = case['paper']
            after = self.c._enumeration_detail(text(f'{name}.html'), case['item'], case['item']['url'])
            if before['published_date'] is None and after['published_date'] is not None:
                filled = dict(after, published_date=None)
                self.assertEqual(json.dumps(before, ensure_ascii=False), json.dumps(filled, ensure_ascii=False), name)
            else:
                self.assertEqual(json.dumps(before, ensure_ascii=False), json.dumps(after, ensure_ascii=False), name)
            self.assertEqual(before['post_number'], after['post_number'])
            self.assertEqual(before['url'], after['url'])

    # (2) the 12 direct PDF media results are now listed ----------------------------------
    def test_missed_media_results_now_listed(self):
        rows = self.g['_parse_search_response'](self.page1, 1)
        self.assertEqual(20, len(rows))
        media = [r for r in rows if self.g['_is_media_url'](r['url'])]
        self.assertEqual([f'{BASE}/media/{i}/download?inline' for i in MEDIA_IDS], [r['url'] for r in media])
        self.assertEqual('Program_v1.pdf', media[0]['title'])
        self.assertEqual('acs177831.pdf', media[1]['title'])
        self.assertEqual(LAYOUT, ''.join('M' if self.g['_is_media_url'](r['url']) else 'H' for r in rows))
        self.assertEqual(rows, self.c._enumeration_items(self.page1))

    def test_media_identity_follows_existing_rule(self):
        item = {'title': 'acs177831.pdf', 'url': f'{BASE}/media/1137/download?inline'}
        paper = self.c._enumeration_detail('%PDF-1.7 ...', item, item['url'], text('media_1137_pdftotext.txt'))
        # crawl(): external_id = node id, else the URL path without the base; post_number = node id or None.
        self.assertEqual('media/1137/download?inline', paper['external_id'])
        self.assertIsNone(paper['post_number'])
        self.assertEqual(item['url'], paper['url'])
        self.assertEqual(item['url'], paper['pdf_url'])
        self.assertEqual('acs177831.pdf', paper['title'])
        self.assertTrue(paper['abstract'].startswith('LABORATORY TESTWORK TO SUPPORT THE DEVELOPMENT OF URANIUM ISR'))
        self.assertNotIn('\n', paper['abstract'])
        self.assertIsNone(paper['published_date'])

    def test_media_without_text_is_skipped_like_short_abstract(self):
        item = {'title': 'Program_v1.pdf', 'url': f'{BASE}/media/7360/download?inline'}
        for pdf_text in (None, '\x0c', 'short'):
            self.assertIsNone(self.c._enumeration_detail('%PDF-1.7', item, item['url'], pdf_text))

    def test_media_challenge_is_not_a_document(self):
        from crawler.enumeration.http import PermanentFailure
        from tests.test_incremental_accc import DENIAL
        item = {'title': 'x.pdf', 'url': f'{BASE}/media/1/download?inline'}
        with self.assertRaises(PermanentFailure):
            self.c._enumeration_detail(DENIAL, item, item['url'], None)

    # (3) empty publication dates are filled from what the page shows --------------------
    def test_body_publication_line_fills_empty_date(self):
        detail = self.g['_parse_detail'](text('clip_day_2023.html'), 'u')
        self.assertEqual('2023-05-30', detail['published_date'])
        self.assertIsNone(self.expected['papers']['clip_day_2023']['paper']['published_date'])

    def test_byline_fills_when_json_ld_has_no_date(self):
        raw = re.sub(r'"datePublished":\s*"[^"]*",', '', text('news_16th_opalndf.html'))
        self.assertNotIn('datePublished', raw)
        self.assertEqual('2015-09-16', self.g['_parse_detail'](raw, 'u')['published_date'])

    def test_update_line_is_not_a_publication_date(self):
        detail = self.g['_parse_detail'](text('tia_pipeline_accelerator.html'), 'u')
        self.assertEqual('', detail['published_date'])

    def test_json_ld_date_keeps_priority(self):
        raw = text('news_phd_awarded.html').replace('24th January 2018', '1st March 2019')
        self.assertEqual('2018-01-24', self.g['_parse_detail'](raw, 'u')['published_date'])

    # crawl() and the enumeration methods give the same papers ---------------------------
    def test_crawl_matches_enumeration_methods(self):
        details = {case['item']['url']: text(f'{name}.html') for name, case in self.expected['papers'].items()}
        page = self.page1
        # Page 1 rows: the four fixture details stand in for the HTML results, media 1137 has text.
        html_urls = list(details)
        rows = self.g['_parse_search_response'](page, 1)
        fake_rows = []
        for i, row in enumerate(r for r in rows if not self.g['_is_media_url'](r['url'])):
            if i < len(html_urls):
                fake_rows.append({'title': row['title'], 'url': html_urls[i]})
        media_rows = [r for r in rows if self.g['_is_media_url'](r['url'])][:3]
        listing = fake_rows + media_rows
        pdf_texts = {f'{BASE}/media/1137/download?inline': text('media_1137_pdftotext.txt')}
        saved = []
        pages = {0: listing, 1: []}
        # crawl()'s own _fetch_detail: media bytes (stand-in: the URL) -> _pdf_text(bytes).
        with patch.dict(self.g, {
            '_fetch_search_page': lambda n: pages[n],
            '_curl_get': lambda url, **k: details[url],
            '_curl_get_bytes': lambda url, **k: url.encode(),
            '_pdf_text': lambda body: pdf_texts.get(body.decode()),
        }), patch.object(self.c, '_save_paper', side_effect=lambda p: saved.append(copy.deepcopy(p))):
            self.c._last_save_created = True
            self.c.crawl()
        expected = []
        for item in listing:
            if self.g['_is_media_url'](item['url']):
                p = self.c._enumeration_detail(item['url'], item, item['url'], pdf_texts.get(item['url']))
            else:
                p = self.c._enumeration_detail(details[item['url']], item, item['url'])
            if p is not None:
                expected.append(p)
        self.assertEqual(5, len(expected))  # 4 HTML + media 1137; media without text skipped
        self.assertEqual(expected, saved)

    def test_crawl_media_path_runs_pdftotext_on_downloaded_bytes(self):
        seen = {}

        def pdf_text(body):
            seen['body'] = body
            return text('media_1137_pdftotext.txt')
        with patch.dict(self.g, {'_curl_get_bytes': lambda url, **k: b'%PDF-1.7 bytes', '_pdf_text': pdf_text,
                                 '_curl_get': lambda *a, **k: self.fail('media must not use the text fetch')}):
            d = self.g['_fetch_detail'](f'{BASE}/media/1137/download?inline')
        self.assertEqual(b'%PDF-1.7 bytes', seen['body'])
        self.assertTrue(d['abstract'].startswith('LABORATORY TESTWORK'))
        self.assertEqual('', d['node_id'])

    def test_pdf_text_args_match_plan(self):
        self.assertEqual(['-enc', 'UTF-8'], self.g['_PDF_TEXT_ARGS'])
        self.assertEqual(30, self.g['_PDF_TEXT_TIMEOUT'])


if __name__ == '__main__':
    unittest.main()
