"""Synthetic Cerema fixtures only: no crawler constructor, fetch, save or DB."""
import importlib.util
import json
import socket
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler.generic_crawler import GenericCrawler
from crawler.document_dates import normalize_document_dates
from crawler.libertree_adapter import paper_to_document

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('cerema_date_regression', ROOT / 'crawler/sites/custom/cerema-fr-fr.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# Explicit expected aliases, independent of the production table.
MONTHS = (
    ('janvier', 'janv', 'jan', 'january'),
    ('février', 'fevrier', 'févr', 'fevr', 'fév', 'fev', 'february', 'feb'),
    ('mars', 'march', 'mar'),
    ('avril', 'avr', 'april', 'apr'),
    ('mai', 'may'),
    ('juin', 'june', 'jun'),
    ('juillet', 'juil', 'july', 'jul'),
    ('août', 'aout', 'aoû', 'aou', 'august', 'aug'),
    ('septembre', 'sept', 'sep', 'september'),
    ('octobre', 'oct', 'october'),
    ('novembre', 'nov', 'november'),
    ('décembre', 'decembre', 'déc', 'dec', 'december'),
)

def listing(day='25', month='Jun', year='2026', *, split=True):
    date = (f'<span class="day">{day}</span><span class="month">{month}</span>'
            f'<span class="year">{year}</span>') if split else f'{day} {month} {year}'
    return ('<div class="view-document-reference-press"><div class="view-content">'
            '<div class="views-row"><article class="node--123">'
            '<h2><a href="/fr/presse/document/synthetic-date">Synthetic</a></h2>'
            f'<div class="publication-date">{date}</div>'
            '</article></div></div></div>')


class CeremaDateRegression(unittest.TestCase):
    def setUp(self):
        for name in ('socket.socket.connect', 'socket.create_connection', 'sqlite3.connect'):
            guard = patch(name, side_effect=AssertionError('network/DB forbidden'))
            guard.start()
            self.addCleanup(guard.stop)
        self.custom = object.__new__(MODULE.CeremaFrFrCrawler)
        self.generic = object.__new__(GenericCrawler)
        self.generic._config = json.loads((ROOT / 'crawler/sites/configs/cerema-fr.json').read_text())

    def generic_items(self, html):
        return self.generic._extract_list_items(BeautifulSoup(html, 'html.parser'), self.generic._config['list_page']['selectors'])

    def test_jun_jul_source_spans_reach_custom_published_date(self):
        for month, expected in [('Jun', '2026-06-25'), ('Jul', '2026-07-25')]:
            with self.subTest(month=month):
                self.assertEqual(self.custom._parse_list_page(listing(month=month))[0]['published_date'], expected)

    def test_twelve_months_french_english_case_and_dot(self):
        for number, aliases in enumerate(MONTHS, 1):
            for alias in aliases:
                for label in (alias, alias.upper(), alias.capitalize(), alias + '.', alias.upper() + '.'):
                    with self.subTest(label=label):
                        self.assertEqual(self.custom._parse_fr_date(' 9 ', ' ' + label + ' ', ' 2026 '), f'2026-{number:02d}-09')

    def test_unsupported_month_is_not_guessed_by_prefix(self):
        for month in ('Juniper', 'Janus', 'Marsupial', 'Octoberfest', 'unknown', '13', '', None):
            with self.subTest(month=month):
                self.assertEqual(self.custom._parse_fr_date('12', month, '2026'), '')

    def test_leap_year_and_century_calendar_boundaries(self):
        for year, expected in [('2024', '2024-02-29'), ('2000', '2000-02-29'), ('1900', ''), ('2025', '')]:
            with self.subTest(year=year):
                self.assertEqual(self.custom._parse_fr_date('29', 'février', year), expected)
        self.assertEqual(self.custom._parse_fr_date('31', 'April', '2026'), '')
        self.assertEqual(self.custom._parse_fr_date('0', 'Jun', '2026'), '')

    def test_year_is_complete_and_never_inferred(self):
        for year in ('26', '2026x', '0000', '10000', '', None):
            with self.subTest(year=year):
                self.assertEqual(self.custom._parse_fr_date('25', 'Jun', year), '')
        self.assertEqual(self.custom._parse_fr_date('1', 'Jan', '0001'), '0001-01-01')

    def test_config_reads_visible_date_raw_without_inventing_listed_date(self):
        item = self.generic_items(listing())[0]
        self.assertEqual(item['published_date'], '25Jun2026')
        self.assertNotIn('listed_date', item)
        self.assertEqual(self.generic._config['list_page']['selectors']['date'], '.publication-date')

    def test_split_span_raw_survives_as_unrecognized_without_generic_change(self):
        item = self.generic_items(listing())[0]
        normalized = normalize_document_dates({'site_id': 'cerema-fr', 'published_date': item['published_date']})
        self.assertEqual(normalized['published_date_raw'], '25Jun2026')
        self.assertIsNone(normalized['published_date'])
        self.assertEqual(normalized['published_date_status'], 'unrecognized')
        self.assertEqual(normalized['listed_date_status'], 'missing')

    def test_unsplit_date_normalizes_without_supplying_listed_date(self):
        item = self.generic_items(listing(split=False))[0]
        normalized = normalize_document_dates({'site_id': 'cerema-fr', 'published_date': item['published_date']})
        self.assertEqual(normalized['published_date'], '2026-06-25')
        self.assertEqual(normalized['published_date_raw'], '25 Jun 2026')
        self.assertEqual(normalized['listed_date_status'], 'missing')

    def test_generic_different_detail_date_does_not_overwrite_listing(self):
        item = self.generic_items(listing(split=False))[0]
        detail = '<main><h1>Synthetic</h1><p>Publié le 27/06/2026</p><time datetime="2026-06-27">27 juin 2026</time></main>'
        self.generic._fetch_page = lambda *args, **kwargs: BeautifulSoup(detail, 'html.parser')
        paper = self.generic._fetch_detail(item['detail_url'], item, self.generic._config['detail_page'])
        self.assertEqual(paper['published_date'], '25 Jun 2026')
        self.assertNotIn('listed_date', paper)
        self.assertEqual(paper_to_document(paper)['posted_date'], None)

    def test_custom_different_detail_date_keeps_existing_list_date_mapping(self):
        item = self.custom._parse_list_page(listing())[0]
        detail = '<main><article class="node--123"><h1>Synthetic</h1><p>Publié le 27/06/2026</p></article></main>'
        paper = self.custom._parse_detail(detail, item)
        self.assertEqual(paper['published_date'], '2026-06-25')
        self.assertEqual(paper['listed_date'], '2026-06-25')

    def test_missing_dates_are_not_filled_from_title_or_detail(self):
        item = self.custom._parse_list_page(listing(month='Unknown'))[0]
        paper = self.custom._parse_detail('<main><article><h1>Report 2026</h1><p>Publié le 27/06/2026</p></article></main>', item)
        self.assertEqual(paper['published_date'], '')
        self.assertEqual(paper['listed_date'], '')

    def test_identity_unaffected_by_month_alias(self):
        june = self.custom._parse_list_page(listing(month='juin'))[0]
        english = self.custom._parse_list_page(listing(month='Jun'))[0]
        self.assertEqual(june, english)


if __name__ == '__main__':
    unittest.main()
