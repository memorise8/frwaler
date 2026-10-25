"""tigta-gov-reports: _enumeration_items / _enumeration_paper give exactly the papers crawl()
saves (crawl() unchanged; post_number = report_number, else None with external_id = PDF stem).
Minimal hand-written JSON / PDF text."""
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load

SITE = 'tigta-gov-reports'
DATA = {'reports': [
    {'link': '/sites/default/files/reports/2026-05/202630012fr.pdf', 'title': 'Audit Alpha',
     'report_number': '2026-300-12', 'report_type': 'Audit', 'date': 'May 28, 2026'},
    {'link': '/sites/default/files/reports/2026-05/202630012fr.pdf', 'title': 'Duplicate link',
     'report_number': '2026-300-99', 'report_type': 'Audit', 'date': 'May 28, 2026'},
    {'link': '', 'title': 'No link'},
    {'link': '/sites/default/files/reports/2025-01/memo-beta.pdf', 'title': '  Memo Beta ',
     'report_number': '', 'report_type': 'Inspection', 'date': 'Jan 3, 2025'},
    {'link': '/sites/default/files/reports/2024-02/short.pdf', 'title': 'Short text',
     'report_number': '2024-1', 'report_type': 'Audit', 'date': 'February 1, 2024'},
    {'link': '/sites/default/files/reports/x.pdf', 'title': ''},
]}
FOUND = ('HIGHLIGHTS\n\nWhat TIGTA Found\n\nThe IRS did not consistently process the returns, '
         'which affected a large number of taxpayers in the period reviewed.\n\nWhat TIGTA Recommended\n\nx')
TEXTS = {
    'https://www.tigta.gov/sites/default/files/reports/2026-05/202630012fr.pdf': FOUND,
    'https://www.tigta.gov/sites/default/files/reports/2025-01/memo-beta.pdf': FOUND.replace('IRS', 'Agency'),
    'https://www.tigta.gov/sites/default/files/reports/2024-02/short.pdf': 'tiny',
}


class Split(unittest.TestCase):
    def setUp(self):
        self.crawler, _ = load(SITE)

    def native(self):
        saved = []
        c = self.crawler
        c._delay = 0
        c._last_save_created = True
        cls = type(c)
        with patch.object(cls, '_curl_get', lambda self, url, *a, **k: json.dumps(DATA)), \
                patch.object(cls, '_download_pdf', lambda self, url, *a, **k: url), \
                patch.object(cls, '_pdf_to_text', lambda self, path, pages=3: TEXTS[path]), \
                patch('os.unlink', lambda p: None), \
                patch.object(cls, '_save_paper', lambda self, p: saved.append(p), create=True):
            c.crawl()
        return saved

    def test_items_apply_crawl_skips(self):
        items = self.crawler._enumeration_items(DATA)
        self.assertEqual(['Audit Alpha', 'Memo Beta', 'Short text'], [i['title'] for i in items])
        self.assertEqual('2026-05-28', items[0]['published_date'])
        self.assertEqual([], self.crawler._enumeration_items({'reports': []}))

    def test_papers_match_crawl(self):
        items = self.crawler._enumeration_items(DATA)
        papers = [self.crawler._enumeration_paper(TEXTS[i['url']], i) for i in items]
        self.assertIsNone(papers[2])
        self.assertIsNone(self.crawler._enumeration_paper(None, items[0]))  # PDF too small / failed
        self.assertEqual(self.native(), [p for p in papers if p])
        self.assertEqual(('2026-300-12', '2026-300-12'), (papers[0]['external_id'], papers[0]['post_number']))
        self.assertEqual(('memo-beta', None), (papers[1]['external_id'], papers[1]['post_number']))
        self.assertEqual('2025-01-03', papers[1]['published_date'])


if __name__ == '__main__':
    unittest.main()
