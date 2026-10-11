import contextlib,importlib.util,io,json,sqlite3,unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler import db,db_libertree
from crawler.base_crawler import CrawlCancelled
ROOT=Path(__file__).resolve().parents[1]
SP=importlib.util.spec_from_file_location('centralbank_scopes',ROOT/'crawler/sites/custom/centralbank-ie-publication.py')
M=importlib.util.module_from_spec(SP);SP.loader.exec_module(M)
F=ROOT/'tests/fixtures/centralbank_original_scopes'
class CentralbankOriginalScopeTests(unittest.TestCase):
 def setUp(self):
  self.conn=sqlite3.connect(':memory:');self.conn.row_factory=sqlite3.Row;self.addCleanup(self.conn.close)
  db.init_db(self.conn);db.register_site(self.conn,M._SITE_ID,M._SITE_ID,M._BASE)
  self.obj=M.CentralBankIEPublicationCrawler(self.conn,delay=0)
 def fixture(self,n):return (F/n).read_text()
 def test_all25_annual_identity_and_fields_preserved_without_generated_abstract(self):
  records=M._records(self.fixture('annual.html'));before=json.loads(self.fixture('expected-annual-before.json'))
  with patch.object(M,'_fetch_detail_text',return_value=''):
   after=[self.obj._pdf_paper(r,'annual') for r in records]
  self.assertEqual(len(after),25)
  for old,new in zip(before,after):
   skip={'abstract','metadata'}
   self.assertEqual({k:v for k,v in old.items() if k not in skip},{k:v for k,v in new.items() if k not in skip})
   self.assertEqual(new['abstract'],'');self.assertIn('full text pending',json.loads(new['metadata'])['abstract_source'])
 def test_actual_first_last_pages_and_adjacent_empty(self):
  first,n=self.obj._parse_press_page(self.fixture('press-page1.html'),1);self.assertEqual(len(first),10);self.assertEqual(n,2)
  last,n=self.obj._parse_press_page(self.fixture('press-page132.html'),132);self.assertEqual(len(last),10);self.assertIsNone(n)
  self.assertEqual(self.obj._parse_press_page(self.fixture('press-page133.html'),133),([],None))
  for item,file,date in [(first[0],'press-first.html','2026-10-02'),(last[-1],'press-last.html','2016-01-11')]:
   with patch.object(self.obj,'_fetch',return_value=self.fixture(file)):p=self.obj._press_paper(item)
   self.assertEqual(p['published_date'],date);self.assertGreater(len(p['abstract']),3000)
   self.assertNotIn('Filter News',p['abstract']);self.assertNotIn('More Articles',p['abstract'])
   self.assertTrue(p['external_id'].startswith('press:/news/article/'))
 def test_archive14_are_compilations_and_index_date_not_publication(self):
  records=M._records(self.fixture('archive.html'));self.assertEqual(len(records),14)
  for r in records:
   p=self.obj._pdf_paper(r,'archive');self.assertIsNone(p['published_date']);self.assertEqual(p['abstract'],'')
   self.assertTrue(p['external_id'].startswith('press-archive:'));self.assertEqual(p['listed_date'],M._source_date(r['date']))
 def test_missing_or_ambiguous_source_is_error(self):
  with self.assertRaises(ValueError):M._records('<html>access denied</html>')
  raw=self.fixture('press-page1.html').replace('/news-media/press-releases/2','/news-media/press-releases/4')
  with self.assertRaises(ValueError):self.obj._parse_press_page(raw,1)
  with self.assertRaises(ValueError):self.obj._parse_press_page('<h1>Access denied</h1>',1)
 def test_source_list_changed_since_cursor_fails_without_saving(self):
  self.obj.delivery_cursor=dict(version=1,scope='press',page=1,next_index=1,list_digest='old-list')
  with patch.object(self.obj,'_fetch',return_value=self.fixture('press-page1.html')):
   with self.assertRaises(ValueError):self.obj.crawl()
  self.assertFalse(self.obj.delivery_exhausted);self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0],0)
 def test_cancel_after_save_reopen_cursor_avoids_refetch_of_saved_detail(self):
  items,_=self.obj._parse_press_page(self.fixture('press-page132.html'),132)
  self.obj.delivery_cursor=dict(version=1,scope='press',page=132,next_index=9,list_digest='')
  def fetch(url):
   if url==M._PRESS_URL+'/132':return self.fixture('press-page132.html')
   if url==items[-1]['url']:return self.fixture('press-last.html')
   if url==M._ARCHIVE_URL:return self.fixture('archive.html')
   raise AssertionError(url)
  self.obj.delivery_should_cancel=lambda:self.conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0]>=1
  with patch.object(M,'_curl_get',side_effect=fetch):
   with self.assertRaises(CrawlCancelled):self.obj.crawl()
  cursor=self.obj.delivery_pending_cursor;self.assertEqual(cursor['scope'],'archive')
  self.assertFalse(self.obj.delivery_exhausted)
  old=[dict(x) for x in self.conn.execute('SELECT * FROM documents')]
  resumed=M.CentralBankIEPublicationCrawler(self.conn,delay=0);resumed.delivery_cursor=cursor
  with patch.object(M,'_curl_get',side_effect=fetch) as network:self.assertEqual(resumed.crawl(),14)
  self.assertEqual([x.args[0] for x in network.call_args_list],[M._ARCHIVE_URL]);self.assertTrue(resumed.delivery_exhausted)
  self.assertEqual(old,[dict(x) for x in self.conn.execute('SELECT * FROM documents')][:1])
 def test_actual_press_and_archive_save_repeat_both_schemas(self):
  items,_=self.obj._parse_press_page(self.fixture('press-page132.html'),132)
  with patch.object(self.obj,'_fetch',return_value=self.fixture('press-last.html')):paper=self.obj._press_paper(items[-1])
  papers=[paper]+[self.obj._pdf_paper(x,'archive') for x in M._records(self.fixture('archive.html'))]
  for kind in ['legacy','v2']:
   c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close)
   if kind=='legacy':db.init_db(c);db.register_site(c,M._SITE_ID,M._SITE_ID,M._BASE)
   else:db_libertree.init_db(c);c.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(M._SITE_ID,M._SITE_ID,M._BASE));c.commit()
   obj=M.CentralBankIEPublicationCrawler(c,delay=0)
   for p in papers:obj._save_paper(p);self.assertTrue(obj._last_save_created)
   rows=[dict(x) for x in c.execute('SELECT * FROM documents')]
   for p in papers:obj._save_paper(p);self.assertFalse(obj._last_save_created)
   self.assertEqual(len(rows),15);self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')])
 def test_zero_limit_and_precancelled_do_not_fetch(self):
  with patch.object(M,'_curl_get',side_effect=AssertionError('unexpected request')):
   self.assertEqual(self.obj.crawl(limit=0),0);self.obj.delivery_should_cancel=lambda:True
   with self.assertRaises(CrawlCancelled):self.obj.crawl()
if __name__=='__main__':unittest.main()
