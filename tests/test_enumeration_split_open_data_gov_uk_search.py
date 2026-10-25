"""data-gov-uk-search: _enumeration_items / _enumeration_detail give exactly the paper crawl()
saves from the HTML search row + package_show JSON (crawl() unchanged; post_number = package id).
Minimal hand-written data."""
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'data-gov-uk-search'
START = ('https://www.data.gov.uk/search?q=&filters%5Bpublisher%5D=&filters%5Btopic%5D='
         '&filters%5Bformat%5D=PDF&sort=best')
PID = '1d3a8981-8080-43d8-b6ec-1bca859d984f'
LIST = ('<html><body><div class="dgu-results__result">'
        f'<h2><a href="/dataset/{PID}/ward-profile">Ward profile</a></h2>'
        '<p>Short summary</p><dl><dt>Published by</dt><dd>Borough Council</dd>'
        '<dt>Last updated</dt><dd>15 September 2023</dd></dl></div></body></html>')
PACKAGE = {'success': True, 'result': {
    'id': PID, 'name': 'ward-profile', 'title': 'Ward profile 2013',
    'notes': 'Health and wellbeing profile for the ward, covering population, deprivation and services.',
    'metadata_created': '2013-05-01T10:00:00', 'metadata_modified': '2023-09-15T10:00:00',
    'organization': {'title': 'Borough Council', 'name': 'borough-council'},
    'resources': [{'format': 'PDF', 'url': 'https://example.gov.uk/files/ward-profile.pdf', 'name': 'Profile'}],
    'tags': [{'name': 'health'}], 'extras': []}}


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, _ = load(SITE)

    def native(self):
        saved = []
        c = self.crawler
        c._delay = 0
        c._last_save_created = True
        cls = type(c)

        def get(self, url, accept='*/*', timeout=40):
            if url == START:
                return LIST
            if 'package_show' in url:
                return json.dumps(PACKAGE)
            return None

        with patch.object(cls, '_curl_get', get), \
                patch.object(cls, '_curl_head', lambda self, *a, **k: None), \
                patch('time.sleep', lambda s: None), \
                patch.object(cls, '_report_source_failure', lambda self, *a: None, create=True), \
                patch.object(cls, '_check_cancelled', lambda self: None, create=True), \
                patch.object(cls, '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl(limit=1)
        return saved

    def test_items(self):
        items = self.crawler._enumeration_items(LIST, START)
        self.assertEqual(1, len(items))
        self.assertEqual(PID, items[0]['external_id'])
        self.assertTrue(items[0]['detail_api_url'].endswith('package_show?id=' + PID))

    def test_paper_matches_crawl(self):
        item = self.crawler._enumeration_items(LIST, START)[0]
        paper = self.crawler._enumeration_detail(PACKAGE, item)
        self.assertEqual(self.native(), [paper])
        self.assertEqual((PID, PID), (paper['external_id'], paper['post_number']))
        self.assertEqual('https://example.gov.uk/files/ward-profile.pdf', paper['pdf_url'])
        self.assertIsNone(self.crawler._enumeration_detail({'success': False}, item))


if __name__ == '__main__':
    unittest.main()
