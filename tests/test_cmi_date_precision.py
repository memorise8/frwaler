import importlib.util,json,sqlite3,unittest
from pathlib import Path
from unittest.mock import patch
from crawler import db,db_libertree
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cmi_date_precision'
S=importlib.util.spec_from_file_location('cmi_date_precision',ROOT/'crawler/sites/custom/cmi-no-publications.py');M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
class CmiDatePrecisionTests(unittest.TestCase):
 def papers(self):
  obj=object.__new__(M.CmiNoPublicationsCrawler);old=json.loads((F/'expected-before.json').read_text());papers=[]
  for name,p in zip(['forthcoming','month','year'],old):
   with patch.object(M,'_curl_get',return_value=(F/(name+'.html')).read_text()):papers.append(obj._parse_detail(p['url'],p['external_id']))
  return old,papers
 def test_actual_three_date_roles_preserve_nondate_fields(self):
  old,papers=self.papers();ignored={'published_date','posted_date','listed_date','metadata'}
  for before,p,precision,period in zip(old,papers,['year','month','year'],['2026','2026-06','1976']):
   self.assertEqual({k:v for k,v in before.items() if k not in ignored},{k:v for k,v in p.items() if k not in ignored});self.assertIsNone(p['published_date']);self.assertIsNone(p['posted_date']);self.assertIsNone(p['listed_date'])
   meta=json.loads(p['metadata']);prior=json.loads(before['metadata']);self.assertEqual(meta['publication_date_raw'],prior['posted_date']);self.assertNotIn('posted_date',meta);self.assertEqual(meta['publication_date_precision'],precision);self.assertEqual(meta['publication_period'],period)
   self.assertEqual({k:meta[k] for k in prior if k!='posted_date'},{k:v for k,v in prior.items() if k!='posted_date'})
  self.assertEqual(json.loads(papers[0]['metadata'])['publication_status'],'forthcoming')
 def test_real_calendar_day_only_and_future_status_not_publication(self):
  self.assertEqual(M._parse_month_year('Journal Article | 29 Feb 2024'),'2024-02-29')
  self.assertIsNone(M._parse_month_year('Journal Article | forthcoming 29 Feb 2024'))
  for raw in ['31 Feb 2024','29 Feb 2023','Janfoo 2024','2024-2025','0000','Jun 2026','1976','']:
   self.assertIsNone(M._parse_month_year(raw))
 def test_all_three_actual_papers_new_repeat_both_owned_schemas(self):
  _,papers=self.papers()
  for kind in ['legacy','v2']:
   c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close);cls=M.CmiNoPublicationsCrawler
   if kind=='legacy':db.init_db(c);db.register_site(c,cls.site_id,cls.site_id,cls.base_url)
   else:db_libertree.init_db(c);c.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(cls.site_id,cls.site_id,cls.base_url));c.commit()
   obj=cls(c,delay=0)
   for p in papers:obj._save_paper(p);self.assertTrue(obj._last_save_created)
   rows=[dict(x) for x in c.execute('SELECT * FROM documents')]
   for p in papers:obj._save_paper(p);self.assertFalse(obj._last_save_created)
   self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')]);self.assertEqual(len(rows),3)
   self.assertTrue(all(not x.get('published_date') and not x.get('posted_date') and not x.get('listed_date') for x in rows))
if __name__=='__main__':unittest.main()
