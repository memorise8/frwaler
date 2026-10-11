import importlib.util
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from crawler import db

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('cass_journal_pagination',ROOT/'crawler/sites/custom/casseng-cssn-cn-sky_research.py')
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CLASS=next(v for k,v in vars(MODULE).items() if k.endswith('Crawler') and k!='BaseCrawler')
FIXTURE=ROOT/'tests/fixtures/cass_journal_pages'


class CassJournalPaginationTests(unittest.TestCase):
    def setUp(self):
        self.conn=sqlite3.connect(':memory:')
        self.conn.row_factory=sqlite3.Row
        self.addCleanup(self.conn.close)
        db.init_db(self.conn)
        db.register_site(self.conn,CLASS.site_id,CLASS.site_id,CLASS.base_url)
        self.crawler=CLASS(self.conn,delay=0)
        self.lookup={MODULE._LIST_URL if p==0 else MODULE._LIST_URL+f'index_{p}.shtml':(FIXTURE/f'page{p}.html').read_text() for p in range(11)}

    def test_all11_actual_pages_match85_reference_URLs_and_preserve_first8(self):
        with patch.object(self.crawler,'_curl_get',side_effect=self.lookup.__getitem__) as fetch:
            pages=list(self.crawler._iter_list_pages())
        rows=[item for page,items in pages for item in items]
        self.assertEqual(rows,json.loads((FIXTURE/'expected-all85.json').read_text()))
        self.assertEqual(rows[:8],json.loads((FIXTURE/'expected-before-first8.json').read_text()))
        self.assertEqual(len(pages),11)
        self.assertEqual(fetch.call_count,11)

    def test_limit_boundary_does_not_prefetch_later_pages(self):
        stop=False
        with patch.object(self.crawler,'_curl_get',side_effect=self.lookup.__getitem__) as fetch:
            pages=self.crawler._iter_list_pages(lambda:stop)
            self.assertEqual(next(pages)[0],0)
            stop=True
            self.assertEqual(list(pages),[])
        self.assertEqual(fetch.call_count,1)

    def test_failed_or_changed_page_is_explicit_failure(self):
        first=self.lookup[MODULE._LIST_URL]
        for second in [None,self.lookup[MODULE._LIST_URL+'index_1.shtml'].replace('countPage = 11','countPage = 12')]:
            with self.subTest(second_is_none=second is None),patch.object(self.crawler,'_curl_get',side_effect=[first,second]):
                pages=self.crawler._iter_list_pages()
                next(pages)
                with self.assertRaises((RuntimeError,ValueError)):
                    next(pages)

    def test_actual_last_page_detail_identity_and_fields_unchanged(self):
        row=json.loads((FIXTURE/'expected-all85.json').read_text())[-1]
        paper=self.crawler._parse_detail(row,row['url'],(FIXTURE/'japanese-studies-detail.html').read_text())
        self.assertEqual(paper,json.loads((FIXTURE/'expected-japanese-detail.json').read_text()))


if __name__=='__main__':
    unittest.main()
