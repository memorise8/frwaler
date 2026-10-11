import importlib.util,json,sqlite3,unittest,contextlib,io
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler import db,db_libertree
from crawler.base_crawler import CrawlCancelled
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cae_pdf_only'
SP=importlib.util.spec_from_file_location('cae_pdf_only',ROOT/'crawler/sites/custom/cae-eco-fr-presse-cae-0.py');M=importlib.util.module_from_spec(SP);SP.loader.exec_module(M)
class CaePdfOnlyTests(unittest.TestCase):
 def setup_crawler(self,kind='legacy'):
  c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close)
  sid=M.CaeEcoFrPresseCae0Crawler.site_id
  if kind=='legacy':db.init_db(c);db.register_site(c,sid,sid,M.BASE)
  else:db_libertree.init_db(c);c.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(sid,sid,M.BASE));c.commit()
  return c,M.CaeEcoFrPresseCae0Crawler(c,delay=0)
 def source_lookup(self):
  manifest=json.loads((F/'sources.json').read_text());lookup={v['url']:(F/k).read_text() for k,v in manifest.items() if 'url' in v}
  s=BeautifulSoup((F/'list.html').read_text(),'lxml')
  for card in list(s.select('article.portfolio-item')):
   if M.BASE+'/'+card.select_one('h3 a')['href'] not in lookup:card.decompose()
  lookup[M.BASE+'/Presse-CAE-0']=str(s);lookup[M.BASE+'/Presse-CAE-1']=(F/'empty-list.html').read_text();return lookup
 def run_crawl(self,obj,lookup,limit=None):
  with patch.object(M,'_curl',side_effect=lookup.__getitem__),patch.object(M.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):return obj.crawl(limit=limit)
 def test_actual_pdf_only_entries_saved_with_source_links_without_fabricated_summary(self):
  c,obj=self.setup_crawler();papers=[];save=obj._save_paper
  def capture(p):papers.append(p);return save(p)
  with patch.object(obj,'_save_paper',side_effect=capture):self.assertEqual(self.run_crawl(obj,self.source_lookup()),3)
  self.assertEqual([len(p['abstract']) for p in papers[1:]],[0,0])
  self.assertEqual(len(json.loads(papers[1]['metadata'])['source_pdf_urls']),2)
  self.assertTrue(all(p['pdf_url'] for p in papers));self.assertEqual(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0],3)
  old=json.loads((F/'expected-first-before.json').read_text());new=papers[0]
  self.assertEqual({k:v for k,v in old.items() if k!='metadata'},{k:v for k,v in new.items() if k!='metadata'})
 def test_actual_three_new_repeat_both_schemas(self):
  for kind in ['legacy','v2']:
   c,obj=self.setup_crawler(kind);self.assertEqual(self.run_crawl(obj,self.source_lookup()),3)
   rows=[dict(x) for x in c.execute('SELECT * FROM documents')];self.assertEqual(self.run_crawl(obj,self.source_lookup()),0)
   self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')])
 def test_cancel_after_actual_save_then_replay_preserves_existing_row(self):
  c,obj=self.setup_crawler();obj.delivery_should_cancel=lambda:c.execute('SELECT COUNT(*) FROM documents').fetchone()[0]>=1
  with self.assertRaises(CrawlCancelled):self.run_crawl(obj,self.source_lookup())
  row=dict(c.execute('SELECT * FROM documents').fetchone());obj.delivery_should_cancel=lambda:False
  self.assertEqual(self.run_crawl(obj,self.source_lookup()),2);self.assertEqual(row,dict(c.execute('SELECT * FROM documents ORDER BY id LIMIT 1').fetchone()))
 def test_zero_limit_and_precancelled_do_not_fetch(self):
  c,obj=self.setup_crawler()
  with patch.object(M,'_curl',side_effect=AssertionError('unexpected request')):
   self.assertEqual(obj.crawl(limit=0),0);obj.delivery_should_cancel=lambda:True
   with self.assertRaises(CrawlCancelled):obj.crawl()
if __name__=='__main__':unittest.main()
