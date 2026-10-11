import contextlib, importlib.util, io, json, sqlite3, unittest
from pathlib import Path
from unittest.mock import patch
from crawler import db, db_libertree
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cepii_date_precision'
S=importlib.util.spec_from_file_location('cepii_date_precision',ROOT/'crawler/sites/custom/cepii-fr-cepii.py');M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
class CepiiDatePrecisionTests(unittest.TestCase):
    def obj(self,kind='legacy'):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close);cls=M.CepiiFrCepiiCrawler
        if kind=='legacy':db.init_db(c);db.register_site(c,cls.site_id,cls.site_id,cls.base_url)
        else:db_libertree.init_db(c);c.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(cls.site_id,cls.site_id,cls.base_url));c.commit()
        return c,cls(c,delay=0)
    def crawl_actual_one(self,obj):
        item=next(x for x in obj._parse_list_page((F/'list.html').read_text()) if x['noDoc']=='4852');papers=[];save=obj._save_paper
        def record(p):papers.append(p);return save(p)
        with patch.object(obj,'_parse_list_page',return_value=[item]),patch.object(obj,'_curl_get',side_effect=lambda u:(F/'detail.html').read_text() if u==item['url'] else (F/'list.html').read_text()),patch.object(obj,'_save_paper',side_effect=record),patch.object(M.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):count=obj.crawl()
        return count,papers
    def test_actual_month_citation_preserves_every_nondate_field(self):
        _,obj=self.obj();_,papers=self.crawl_actual_one(obj);p=papers[0];old=json.loads((F/'expected-before.json').read_text());ignored={'published_date','listed_date','metadata'}
        self.assertEqual({k:v for k,v in p.items() if k not in ignored},{k:v for k,v in old.items() if k not in ignored})
        self.assertIsNone(p['published_date']);self.assertIsNone(p['listed_date']);meta=json.loads(p['metadata']);before=json.loads(old['metadata'])
        self.assertEqual({k:meta[k] for k in before if k!='posted_date'},{k:v for k,v in before.items() if k!='posted_date'});self.assertNotIn('posted_date',meta);self.assertEqual(meta['publication_date_raw'],before['posted_date']);self.assertEqual(meta['publication_date_raw'],'décembre 2012');self.assertEqual(meta['publication_date_precision'],'month');self.assertEqual(meta['publication_period'],'2012-12')
    def test_partial_dates_are_not_promoted_to_first_day(self):
        for raw,precision,period in [('décembre 2012','month','2012-12'),('2012','year','2012')]:
            self.assertEqual(M._publication_date(raw),(None,precision,period));self.assertEqual(M._parse_fr_date(raw),'')
    def test_explicit_days_are_validated_without_guessing_unknown_months(self):
        self.assertEqual(M._publication_date('29 février 2024'),('2024-02-29','day','2024-02-29'))
        self.assertEqual(M._parse_fr_date('1er mars 2024'),'2024-03-01')
        for raw in ['31 février 2024','29 février 2023','inconnu 2012','0000','2012-2013','']:
            self.assertEqual(M._parse_fr_date(raw),'')
    def test_actual_record_new_repeat_both_schemas(self):
        for kind in ['legacy','v2']:
            c,obj=self.obj(kind);self.assertEqual(self.crawl_actual_one(obj)[0],1);rows=[dict(x) for x in c.execute('SELECT * FROM documents')]
            self.assertEqual(self.crawl_actual_one(obj)[0],0);self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')]);self.assertFalse(rows[0].get('published_date'));self.assertFalse(rows[0].get('posted_date') or rows[0].get('listed_date'))
if __name__=='__main__':unittest.main()
