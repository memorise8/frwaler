"""Synthetic USGS detail-table fixtures only: no network, no DB."""
import json
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from crawler.document_dates import normalize_document_dates
from crawler.generic_crawler import GenericCrawler

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / 'crawler/sites/configs/pubs-usgs-gov.json').read_text())


def detail(rows):
    body = ''.join(f'<tr><th scope="row">{k}</th><td>{v}</td></tr>' for k, v in rows)
    return ('<main><h3 class="publication-title">Synthetic</h3><section id="details">'
            f'<table class="usa-table usa-table--borderless"><tbody>{body}</tbody></table></section></main>')


class UsgsDetailDate(unittest.TestCase):
    def setUp(self):
        self.crawler = object.__new__(GenericCrawler)
        self.crawler._config = CONFIG

    def parse(self, rows):
        html = detail(rows)
        self.crawler._fetch_page = lambda *a, **k: BeautifulSoup(html, 'html.parser')
        paper = self.crawler._fetch_detail('https://pubs.usgs.gov/publication/x', {'published_date': ''}, CONFIG['detail_page'])
        return paper['published_date']

    def test_isbn_row_before_year_row(self):
        raw = self.parse([('Publication Type', 'Conference Paper'), ('Publication Subtype', 'x'), ('Title', 't'),
                          ('ISBN', '0000000000'), ('Issue', '2'), ('Year Published', '1993'), ('Language', 'English')])
        self.assertEqual(raw, '1993')
        n = normalize_document_dates({'site_id': 'pubs-usgs-gov', 'published_date': raw})
        self.assertEqual((n['published_date_raw'], n['published_date_status'], n['published_date_precision']),
                         ('1993', 'partial', 'year'))

    def test_year_row_in_fourth_position(self):
        raw = self.parse([('Publication Type', 'a'), ('Publication Subtype', 'b'), ('Title', 't'),
                          ('Year Published', '1999'), ('Language', 'English')])
        self.assertEqual(raw, '1999')
        n = normalize_document_dates({'site_id': 'pubs-usgs-gov', 'published_date': raw})
        self.assertEqual((n['published_date_status'], n['published_date_precision']), ('partial', 'year'))

    def test_row_order_independent(self):
        rows = [('ISBN', '1'), ('Year Published', '2001'), ('Language', 'English')]
        self.assertEqual(self.parse(rows), self.parse(rows[::-1]))

    def test_missing_year_row_yields_no_date(self):
        raw = self.parse([('Publication Type', 'a'), ('Publication Subtype', 'b'), ('Title', 't'),
                          ('ISBN', '0872629201'), ('Language', 'English'), ('Issue', '1')])
        self.assertEqual(raw, '')
        n = normalize_document_dates({'site_id': 'pubs-usgs-gov', 'published_date': raw})
        self.assertIsNone(n['published_date'])
        self.assertEqual(n['published_date_status'], 'missing')

    def test_selector_is_label_based(self):
        sel = CONFIG['detail_page']['selectors']['date']
        self.assertNotIn('nth-child', sel)
        self.assertIn('Year Published', sel)


if __name__ == '__main__':
    unittest.main()
