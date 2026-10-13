"""Synthetic CKAN / DHS detail fixtures only: no network, no DB.

Only the label/table structure of the real pages is reproduced.
"""
import json
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from crawler.document_dates import normalize_document_dates
from crawler.generic_crawler import GenericCrawler

ROOT = Path(__file__).resolve().parents[1]
AERO = json.loads((ROOT / 'crawler/sites/configs/data-aeronomie-be.json').read_text())
DHS = json.loads((ROOT / 'crawler/sites/configs/dhs-gov-publications.json').read_text())


def ckan(rows):
    body = ''.join(
        f'<tr>\n<th scope="row" class="dataset-label">{k}</th>\n'
        f'<td class="dataset-details " property="">\n\n<p>{v}</p>\n\n</td>\n</tr>\n' for k, v in rows)
    return ('<div class="primary"><article class="module"><div class="module-content"><h1>Synthetic</h1></div></article>'
            f'<section class="additional-info"><table class="table"><tbody>{body}</tbody></table></section></div>')


def dhs(updated='Last Updated: 01/02/2025', attach_date='03/04/2022'):
    return ('<div class="pane-content"><h1>Synthetic</h1></div><div class="node__content"><p>Body</p></div>'
            '<table><thead><tr><th>Attachment</th><th>Ext.</th><th>Size</th><th>Date</th></tr></thead>'
            f'<tbody><tr><td>file</td><td>pdf</td><td>1 MB</td><td>{attach_date}</td></tr></tbody></table>'
            f'<div>{updated}</div>')


class Base(unittest.TestCase):
    config = None

    def setUp(self):
        self.crawler = object.__new__(GenericCrawler)
        self.crawler._config = self.config

    def parse(self, html):
        self.crawler._fetch_page = lambda *a, **k: BeautifulSoup(html, 'html.parser')
        return self.crawler._fetch_detail('https://example.invalid/x', {'published_date': ''},
                                          self.config['detail_page'])


class AeronomieCreatedDate(Base):
    config = AERO

    def raw(self, rows):
        return self.parse(ckan(rows))['published_date']

    def norm(self, raw):
        return normalize_document_dates({'site_id': 'data-aeronomie-be', 'published_date': raw})

    ROWS = [('Principal', 'x'), ('Contact', 'y'), ('Starting', 'January 1, 2000'), ('Ending', 'December 31, 2001'),
            ('Last Updated', 'July 2, 2024'), ('Created', 'April 25, 2019')]

    def test_created_row_is_published_date_and_normalizes(self):
        raw = self.raw(self.ROWS)
        self.assertEqual(raw, 'April 25, 2019')
        n = self.norm(raw)
        self.assertEqual(n['published_date'], '2019-04-25')
        self.assertEqual(n['published_date_raw'], 'April 25, 2019')
        self.assertEqual((n['published_date_status'], n['published_date_precision']), ('normalized', 'day'))

    def test_row_order_independent(self):
        self.assertEqual(self.raw(self.ROWS), self.raw(self.ROWS[::-1]))
        self.assertEqual(self.raw(self.ROWS), self.raw([self.ROWS[-1]] + self.ROWS[:-1]))

    def test_missing_created_row_yields_no_date(self):
        raw = self.raw([r for r in self.ROWS if r[0] != 'Created'])
        self.assertEqual(raw, '')
        n = self.norm(raw)
        self.assertIsNone(n['published_date'])
        self.assertEqual(n['published_date_status'], 'missing')

    def test_observation_period_and_update_rows_never_used(self):
        raw = self.raw([('Starting', 'May 5, 1999'), ('Ending', 'May 6, 1999'), ('Last Updated', 'June 7, 2020')])
        self.assertEqual(raw, '')

    def test_selector_is_label_based(self):
        sel = AERO['detail_page']['selectors']['date']
        self.assertNotIn('nth-child', sel)
        self.assertIn('Created', sel)


class DhsPublishedDateStaysEmpty(Base):
    config = DHS

    def test_no_date_selectors_configured(self):
        self.assertIsNone(DHS['detail_page']['selectors']['date'])
        self.assertIsNone(DHS['list_page']['selectors']['date'])

    def test_attachment_and_update_dates_not_published_date(self):
        paper = self.parse(dhs())
        self.assertEqual(paper['published_date'], '')
        n = normalize_document_dates({'site_id': 'dhs-gov-publications', 'published_date': paper['published_date']})
        self.assertIsNone(n['published_date'])
        self.assertEqual(n['published_date_status'], 'missing')

    def test_generic_detail_has_no_listed_date_field(self):
        # Documents the limit: no config key can populate listed_date/posted_date.
        paper = self.parse(dhs())
        self.assertNotIn('listed_date', paper)
        self.assertNotIn('posted_date', paper)
        self.assertEqual(json.loads(paper['metadata']), {})


if __name__ == '__main__':
    unittest.main()
