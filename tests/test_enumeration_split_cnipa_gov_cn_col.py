"""cnipa-gov-cn-col: last_page_backward plan over the jpage ``dataproxy.jsp?page=N``
listing (col89) + pure conversions (memory only).

Without startrecord/endrecord the proxy answers records (N-1)*100+1..total, so page 1
holds everything and later pages repeat a tail. ``_enumeration_items(xml, page)`` keeps
each page's own 100-record window (disjoint neighbour pages); crawl() reads page 1 and
drops repeats by URL, so the saved set is the same. ``_enumeration_paper(html, item)``
is a pure copy of crawl()'s assembly over a pure copy of ``_fetch_detail``.
"""
import ast
import hashlib
import unittest

from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import ROOT, load
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks

SITE = 'cnipa-gov-cn-col'


def datastore(ids, total, pages):
    records = ''.join('<record><![CDATA[<li><a href="https://www.cnipa.gov.cn/art/2026/1/1/art_89_%d.html">T%d</a>'
                      '<span>2026-01-01</span></li>]]></record>' % (i, i) for i in ids)
    return ('<datastore><totalrecord>%d</totalrecord><totalpage>%d</totalpage><recordset>%s</recordset></datastore>'
            % (total, pages, records))


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_module_helpers_are_unchanged(self):
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fixture['module_function_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(self.SITE)
        for value in ({'error': 'not a listing'}, '<html>maintenance</html>', '<datastore><recordset>x</recordset></datastore>'):
            with self.subTest(value=value):
                with self.assertRaises(PermanentFailure) as ctx:
                    crawler._enumeration_items(value, 1)
                self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_identity_follows_detail_article_id_with_list_fallback(self):
        expected, _ = self.native()
        self.assertEqual(['900001', '900002'], [p['external_id'] for p in expected])
        self.assertEqual(['900001', '900002'], [p['external_id'] for p in self.pipeline()])

    def test_windows_of_the_live_shape_are_disjoint_and_complete(self):
        """215 records, 3 pages: page 1 = all 215, page 2 = last 115, page 3 = last 15."""
        crawler, _ = load(self.SITE)
        ids = list(range(1000, 1215))
        pages = [crawler._enumeration_items(datastore(ids[(n - 1) * 100:], 215, 3), n) for n in (1, 2, 3)]
        keys = [[row['article_id'] for row in rows] for rows in pages]
        self.assertEqual([100, 100, 15], [len(k) for k in keys])
        self.assertEqual([str(i) for i in ids], keys[0] + keys[1] + keys[2])
        self.assertEqual([], crawler._enumeration_items('<datastore></datastore>', 4))

    def test_changed_window_shape_is_a_parse_error(self):
        crawler, _ = load(self.SITE)
        ids = list(range(1000, 1215))
        for page, body in ((2, datastore(ids[50:], 215, 3)), (1, datastore(ids, 215, 5)), (4, datastore(ids[:5], 215, 3))):
            with self.subTest(page=page):
                with self.assertRaises(PermanentFailure) as ctx:
                    crawler._enumeration_items(body, page)
                self.assertEqual('listing_parse_error:unexpected_record_window', ctx.exception.reason)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE


class AttachmentRows(unittest.TestCase):
    def test_attachment_only_rows_are_left_out_of_the_window(self):
        crawler, _ = load(SITE)
        arts = ''.join('<record><![CDATA[<li><a href="https://www.cnipa.gov.cn/art/2026/1/1/art_89_%d.html">T%d</a>'
                       '<span>2026-01-01</span></li>]]></record>' % (i, i) for i in (2001, 2002))
        att = ('<record><![CDATA[<li><a href="/attach/0/0123abcd.doc">Attachment</a>'
               '<span>2010-01-01</span></li>]]></record>')
        xml = ('<datastore><totalrecord>3</totalrecord><totalpage>1</totalpage><recordset>%s%s</recordset></datastore>'
               % (arts, att))
        rows = crawler._enumeration_items(xml, 1)
        self.assertEqual(['2001', '2002'], [r['article_id'] for r in rows])
