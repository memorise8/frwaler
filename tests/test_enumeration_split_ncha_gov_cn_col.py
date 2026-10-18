import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import clean_paper
from crawler.enumeration.http import PermanentFailure, Response
from tests.collector_split_helpers import load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'ncha-gov-cn-col'
LIST_URL = ('http://www.ncha.gov.cn/module/jslib/jquery/jpage/dataproxy.jsp?page={page}&appid=1&webid=1'
            '&path=%2F&columnid=2423&unitid=18241')
DISPOSITION = "attachment; filename*=UTF-8''%E6%96%87%E7%89%A9%E6%A0%87%E5%87%86.pdf"


def jpage(total, page, rows):
    """Synthetic dataproxy answer: records (page-1)*100+1 .. total, as the source sends them."""
    pages = -(-total // 100)
    recs = ''.join(
        f'<record><![CDATA[<tr class="me"><td class="mes"></td><td>WW/T {n}—2026</td>'
        f'<td><a href="/module/download/downfile.jsp?classid=0&amp;filename={8000000 + n}.pdf">Synthetic standard {n}</a></td>'
        f'<td>2026-01-01</td><td>2026-02-01</td><td>推荐性</td><td>现行</td><td>国家文物局</td></tr>]]></record>'
        for n in rows)
    return (f'<datastore><totalrecord>{total}</totalrecord><totalpage>{pages}</totalpage>'
            f'<recordset>{recs}</recordset></datastore>')


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None, headers_text='HTTP/1.1 200 OK\r\nContent-Type: text/html\r\n', moved=False):
        """crawl() with memory transports for its list GET and its per-item HEAD."""
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured = []; calls = []; listing_calls = []
        def get_text(url, *args, **kwargs):
            listing_calls.append(url)
            return fixture['listing_body'] if len(listing_calls) == 1 else fixture['empty_body']
        def get_headers(url, *args, **kwargs):
            self.assertIn(url, fixture['details'])
            calls.append(url)
            return headers_text, url + '?moved=1' if moved else url
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_get_text = get_text
        crawler._curl_get_headers = get_headers
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertLessEqual(len(listing_calls), 2)
        return captured, calls

    def test_detail_redirect_preserves_registered_identity(self):
        """crawl() stores curl's effective (post-redirect) URL as url/pdf_url; the
        framework's final response URL gives the same paper and the same post_number."""
        expected, _ = self.native(moved=True)
        actual = self.pipeline(redirect=True)
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([p['external_id'] for p in self.native()[0]], [p['external_id'] for p in actual])

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(self.SITE)
        cases = [{'error': 'not a listing'}, '<html><body>maintenance</body></html>',
                 '<datastore><recordset><record><![CDATA[<tr></tr>]]></record></recordset></datastore>']
        for bad in cases:
            with self.subTest(bad=bad):
                with self.assertRaises(PermanentFailure) as ctx:
                    crawler._enumeration_items(bad, LIST_URL.format(page=1))
                self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_each_page_keeps_only_its_own_window(self):
        """page=1 answers all 117 records, page=2 the last 17 again; the union has no
        overlap and equals what crawl() reads from page 1 (list_page 1 everywhere)."""
        crawler, _ = load(self.SITE)
        first = crawler._enumeration_items(jpage(117, 1, range(1, 118)).encode(), LIST_URL.format(page=1))
        second = crawler._enumeration_items(jpage(117, 2, range(101, 118)), LIST_URL.format(page=2))
        self.assertEqual(100, len(first)); self.assertEqual(17, len(second))
        self.assertEqual(set(), {r['external_id'] for r in first} & {r['external_id'] for r in second})
        whole, _, _, _ = crawler._parse_list_page(jpage(117, 1, range(1, 118)), 1)
        self.assertEqual(whole, first + second)
        with self.assertRaises(PermanentFailure):  # window no longer matches the observed shape
            crawler._enumeration_items(jpage(117, 2, range(1, 118)), LIST_URL.format(page=2))
        self.assertEqual([], crawler._enumeration_items('<datastore></datastore>', LIST_URL.format(page=3)))

    def test_file_response_headers_feed_the_paper_like_crawls_head(self):
        """Content-Disposition, type and length from the GET response give the same paper
        as crawl()'s HEAD headers."""
        hdrs = {'Content-Disposition': DISPOSITION, 'Content-Type': 'application/pdf', 'Content-Length': '1234',
                'Last-Modified': 'Mon, 01 Jan 2026 00:00:00 GMT', 'ETag': '"abc"'}
        head_text = 'HTTP/1.1 200 OK\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in hdrs.items())
        expected, _ = self.native(headers_text=head_text)
        crawler, _ = load(self.SITE)
        rows = crawler._enumeration_items(self.fixture['listing_body'], LIST_URL.format(page=1))
        actual = [crawler._enumeration_paper(Response(200, hdrs, b'%PDF', r['url']), r) for r in rows]
        self.assertEqual(expected, actual)
        self.assertEqual('文物标准.pdf', actual[0]['original_filename'])


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE
