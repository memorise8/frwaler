"""S-open g2: olympias past-the-end listing page is a confirmed empty page.

DSpace JSPUI answers a start offset beyond the result count with the full search
page and "Results 3021-3003 of 3003" (first > total). Minimal synthetic HTML only.
"""
import unittest

from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure
from tests.collector_split_helpers import load

SITE = 'olympias-lib-uoi-gr-jspui'

LISTING = """<html><body><main><table>
<tr><td><a href="/jspui/handle/123456789/5598">Thesis A</a></td></tr>
<tr><td><a href="/jspui/handle/123456789/6643">Thesis B</a></td></tr>
<tr><td><a href="/jspui/handle/123456789/6643">Thesis B again</a></td></tr>
</table><div>Results 1-20 of 3003</div></main></body></html>"""

PAST_END = """<html><body><main><form>Search Go!</form>
<div>Results 3021-3003 of 3003 (Search time: 0.005 seconds).</div></main></body></html>"""

LAST_PAGE_EMPTY_BUT_IN_RANGE = """<html><body><main>
<div>Results 3001-3003 of 3003</div></main></body></html>"""

DETAIL = """<html><head>
<meta name="citation_pdf_url" content="https://olympias.lib.uoi.gr/jspui/bitstream/123456789/5598/1/a.pdf">
</head><body><table>
<tr><td class="metadataFieldLabel">dc.title</td><td class="metadataFieldValue">Thesis A</td></tr>
<tr><td class="metadataFieldLabel">heal.abstract</td><td class="metadataFieldValue">%s</td></tr>
<tr><td class="metadataFieldLabel">heal.publicationDate</td><td class="metadataFieldValue">2013</td></tr>
<tr><td class="metadataFieldLabel">dc.date.accessioned</td><td class="metadataFieldValue">2014-02-03T10:00:00Z</td></tr>
</table></body></html>""" % ('Synthetic abstract text. ' * 8)


class OlympiasOpen(unittest.TestCase):
    def setUp(self):
        self.c, _ = load(SITE)

    def test_items_keep_handle_urls(self):
        with guarded():
            rows = self.c._enumeration_items(LISTING)
        self.assertEqual(['123456789/5598', '123456789/6643'], [r['handle'] for r in rows])
        self.assertEqual('https://olympias.lib.uoi.gr/jspui/handle/123456789/5598?mode=full', rows[0]['url'])

    def test_past_end_page_is_empty(self):
        with guarded():
            self.assertEqual([], self.c._enumeration_items(PAST_END))

    def test_in_range_page_without_rows_is_an_error(self):
        with self.assertRaises(PermanentFailure):
            with guarded():
                self.c._enumeration_items(LAST_PAGE_EMPTY_BUT_IN_RANGE)
        with self.assertRaises(PermanentFailure):
            with guarded():
                self.c._enumeration_items('<html><body><main>maintenance</main></body></html>')

    def test_paper_identity(self):
        item = {'url': 'https://olympias.lib.uoi.gr/jspui/handle/123456789/5598?mode=full',
                'handle': '123456789/5598'}
        with guarded():
            paper = self.c._enumeration_paper(item, self.c._enumeration_detail(DETAIL, item))
        self.assertEqual('123456789/5598', paper['external_id'])
        self.assertEqual('5598', paper['post_number'])
        self.assertEqual('https://olympias.lib.uoi.gr/jspui/handle/123456789/5598', paper['url'])
        self.assertEqual('Thesis A', paper['title'])
        self.assertEqual('2013-01-01', paper['published_date'])


if __name__ == '__main__':
    unittest.main()
