"""SCAHT's observed current DOM, exercised through the actual generic save path."""
import contextlib
import io
import os
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db_libertree
from crawler.generic_crawler import GenericCrawler

CONFIG=Path(__file__).resolve().parents[1]/'crawler/sites/configs/scaht-org.json'


class ScahtConfigurationTests(unittest.TestCase):
    def test_live_dom_shape_pages_dates_and_repeat_new_counts(self):
        conn=sqlite3.connect(':memory:');self.addCleanup(conn.close)
        db_libertree.init_db(conn);db_libertree.upsert_site(conn,'scaht-org','SCAHT','https://www.scaht.org')
        requested=[]
        def fetch(url,params=None):
            if params is not None:
                self.assertEqual(url,'https://www.scaht.org/en/publications/')
                page=params['page'];requested.append(page)
                records=range((page-1)*3,min(page*3,6))
                html=''.join(f'<div class="publications-item"><p class="publications-date">5 Jun 2026</p><a href="http://scaht.org/en/publications/document-{n}/">Research {n}</a></div>' for n in records)
            else:
                slug=url.rstrip('/').split('/')[-1]
                html=f'<div class="publication-page"><div class="publication-date"><p>June 5, 2026</p></div><div class="publication-title"><h1>{slug}</h1></div><div class="publication-authors"><p>Fixture Author</p></div><div class="publication-link"><a href="https://publisher.invalid/record">Read more</a></div></div>'
            return BeautifulSoup(html,'html.parser')
        with patch.dict(os.environ,{'LIBERTREE_DB_BACKEND':'sqlite'}),contextlib.redirect_stdout(io.StringIO()):
            for expected in (2,4,6):
                c=GenericCrawler(CONFIG,conn);c.delivery_mode='full'
                with patch.object(c,'_fetch_page',side_effect=fetch):self.assertEqual(c.crawl(limit=2),2)
                self.assertEqual(conn.execute('SELECT count(*) FROM documents').fetchone()[0],expected)
            c=GenericCrawler(CONFIG,conn);c.delivery_mode='full'
            with patch.object(c,'_fetch_page',side_effect=fetch):self.assertEqual(c.crawl(limit=2),0)
        self.assertIn(2,requested)
        self.assertEqual(conn.execute('SELECT count(*) FROM documents WHERE published_date=?',('2026-06-05',)).fetchone()[0],6)
        self.assertEqual(conn.execute('SELECT count(*) FROM documents WHERE pdf_url IS NOT NULL AND pdf_url <> ?',('',)).fetchone()[0],0)
        self.assertFalse(c.delivery_exhausted)

    def test_current_public_list_and_detail_evidence_shape(self):
        # The listing date is a sibling inside the publication item, not a
        # relative CSS combinator evaluated from an anchor element.
        c=GenericCrawler(CONFIG,None)
        soup=BeautifulSoup('<div class="publications-item"><p class="publications-date">5 Jun 2026</p><a href="/en/publications/research-slug/">Research title</a></div>','html.parser')
        items=c._extract_list_items(soup,c._config['list_page']['selectors'])
        self.assertEqual(len(items),1)
        self.assertEqual(items[0]['external_id'],'research-slug')
        self.assertEqual(items[0]['published_date'],'5 Jun 2026')
