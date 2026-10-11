import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch
from crawler import db,db_libertree

ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('nci_fiscal_dates',ROOT/'crawler/sites/custom/cancer-gov-about-nci.py')
MODULE=importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE=ROOT/'tests/fixtures/nci_fiscal_dates'


class NciFiscalDateTests(unittest.TestCase):
    def capture(self, raw):
        obj=object.__new__(MODULE.CancerGovAboutNciCrawler)
        papers=[]
        def save(p):
            papers.append(p)
            obj._last_save_created=True
        with patch.object(MODULE,'_curl_get',return_value=raw),patch.object(MODULE.time,'sleep'),patch.object(obj,'_save_paper',side_effect=save),contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(obj.crawl(),25)
        return papers

    def test_all25_actual_identities_and_non_date_fields_unchanged(self):
        before=json.loads((FIXTURE/'expected-before-papers.json').read_text())
        after=self.capture((FIXTURE/'landing.html').read_text())
        for old,new in zip(before,after):
            omitted={'published_date','listed_date','metadata'}
            self.assertEqual({k:v for k,v in old.items() if k not in omitted},{k:v for k,v in new.items() if k not in omitted})
            self.assertIsNone(new['published_date'])
            self.assertIsNone(new['listed_date'])
            metadata=json.loads(new['metadata'])
            self.assertIsNone(metadata['posted_date'])
            self.assertEqual(metadata['fiscal_year_role'],'budget_coverage_period')
            self.assertEqual(metadata['landing_updated_date'],'2026-09-02')
            self.assertEqual(metadata['landing_updated_date_role'],'catalogue_page_update')
            self.assertTrue(metadata['landing_updated_datetime_raw'].startswith('2026-09-02'))
            self.assertIn('2026',metadata['landing_updated_date_raw'])
            prior=json.loads(old['metadata'])
            self.assertEqual({k:metadata[k] for k in prior if k!='posted_date'},{k:v for k,v in prior.items() if k!='posted_date'})
        self.assertEqual(before[0]['published_date'],'2027-01-01')
        self.assertEqual(json.loads(after[0]['metadata'])['fy_year'],'2028')

    def test_changed_catalogue_update_does_not_change_document_dates(self):
        raw=(FIXTURE/'landing.html').read_text().replace('2026-09-02','2026-10-11')
        for paper in self.capture(raw):
            self.assertIsNone(paper['published_date'])
            self.assertIsNone(paper['listed_date'])
            self.assertEqual(json.loads(paper['metadata'])['landing_updated_date'],'2026-10-11')

    def test_all25_save_and_repeat_without_fiscal_or_page_dates_in_both_schemas(self):
        for kind in ['legacy','v2']:
            with self.subTest(schema=kind):
                conn=sqlite3.connect(':memory:')
                conn.row_factory=sqlite3.Row
                self.addCleanup(conn.close)
                cls=MODULE.CancerGovAboutNciCrawler
                if kind=='legacy':
                    db.init_db(conn)
                    db.register_site(conn,cls.site_id,cls.site_id,cls.base_url)
                else:
                    db_libertree.init_db(conn)
                    conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(cls.site_id,cls.site_id,cls.base_url))
                    conn.commit()
                obj=cls(conn,delay=0)
                with patch.object(MODULE,'_curl_get',return_value=(FIXTURE/'landing.html').read_text()),patch.object(MODULE.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(obj.crawl(),25)
                    rows=[dict(x) for x in conn.execute('SELECT * FROM documents')]
                    self.assertEqual(obj.crawl(),0)
                    self.assertEqual(rows,[dict(x) for x in conn.execute('SELECT * FROM documents')])
                self.assertTrue(all(not x['published_date'] and not x.get('listed_date') and not x.get('posted_date') for x in rows))


if __name__=='__main__':
    unittest.main()
