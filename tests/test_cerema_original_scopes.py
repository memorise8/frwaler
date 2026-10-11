import contextlib,hashlib,importlib.util,io,json,sqlite3,unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler import db,db_libertree,storage
from crawler.base_crawler import CrawlCancelled
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cerema_original_scopes'
S=importlib.util.spec_from_file_location('cerema_scopes',ROOT/'crawler/sites/custom/cerema-fr-fr.py');M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
class CeremaOriginalScopesTests(unittest.TestCase):
 def setUp(self):
  self.old=json.loads((F/'expected-before.json').read_text());self.lookup={}
  for label,p in zip(['dossier','document'],self.old):self.lookup[M._LIST_SCOPES[label]]=(F/(label+'-scoped-list.html')).read_text();self.lookup[p['url']]=(F/(label+'-detail.html')).read_text()
 def crawler(self,kind='legacy',conn=None):
  if conn is None:
   conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
   if kind=='legacy':db.init_db(conn);db.register_site(conn,M._SITE_ID,M._SITE_ID,M._BASE_URL)
   else:db_libertree.init_db(conn);conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(M._SITE_ID,M._SITE_ID,M._BASE_URL));conn.commit()
  return conn,M.CeremaFrFrCrawler(conn,delay=0)
 def run_crawl(self,obj,**kwargs):
  with patch.object(obj,'_curl',side_effect=lambda u,**kw:self.lookup[u]),patch.object(M.time,'sleep'),patch.object(storage,'cleanup_doc_files',side_effect=AssertionError('unexpectedcleanup')),contextlib.redirect_stdout(io.StringIO()):return obj.crawl(**kwargs)
 def test_actual_fields_and_existing_press_ID_preserved(self):
  _,obj=self.crawler()
  for label,old in zip(['dossier','document'],self.old):
   items,next_page=obj._parse_source_page(self.lookup[M._LIST_SCOPES[label]],label,0);self.assertIsNone(next_page);p=obj._parse_detail(self.lookup[old['url']],items[0])
   self.assertEqual({k:v for k,v in p.items() if k!='metadata'},{k:v for k,v in old.items() if k!='metadata'})
   meta=json.loads(p['metadata']);prior=json.loads(old['metadata']);prior['list_url']=M._LIST_SCOPES[label];self.assertEqual(meta,prior)
 def test_both_actual_scopes_new_cancel_cursor_resume_repeat_both_schemas(self):
  for kind in ['legacy','v2']:
   c,obj=self.crawler(kind);obj.delivery_should_cancel=lambda:c.execute('SELECT COUNT(*) FROM documents').fetchone()[0]>=1
   with self.assertRaises(CrawlCancelled):self.run_crawl(obj)
   first=dict(c.execute('SELECT * FROM documents').fetchone());cursor=obj.delivery_pending_cursor;self.assertEqual(cursor['scope'],'document')
   _,obj=self.crawler(kind,c);obj.delivery_cursor=cursor
   with patch.object(obj,'_curl',side_effect=lambda u,**kw:self.lookup[u]) as fetch,patch.object(M.time,'sleep'):
    self.assertEqual(obj.crawl(),1);self.assertEqual([x.args[0] for x in fetch.call_args_list],[M._LIST_SCOPES['document'],self.old[1]['url']])
   self.assertTrue(obj.delivery_exhausted);rows=[dict(x) for x in c.execute('SELECT * FROM documents')];self.assertEqual(rows[0],first)
   _,again=self.crawler(kind,c);self.assertEqual(self.run_crawl(again),0);self.assertTrue(again.delivery_exhausted);self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')])
 def test_limit_one_preserves_unvisited_second_scope(self):
  c,obj=self.crawler();self.assertEqual(self.run_crawl(obj,limit=1),1);self.assertFalse(obj.delivery_exhausted)
  cursor=obj.delivery_pending_cursor;_,obj=self.crawler(conn=c);obj.delivery_cursor=cursor;self.assertEqual(self.run_crawl(obj,limit=1),1);self.assertTrue(obj.delivery_exhausted)
 def test_changed_source_digest_fails_before_detail_or_save(self):
  c,obj=self.crawler();items,_=obj._parse_source_page(self.lookup[M._LIST_URL],'dossier',0);digest=hashlib.sha256(json.dumps([x['url'] for x in items],ensure_ascii=False).encode()).hexdigest();obj.delivery_cursor=dict(version=1,scope='dossier',page=0,next_index=0,list_digest=digest)
  soup=BeautifulSoup(self.lookup[M._LIST_URL],'lxml');soup.select_one('.views-row h2 a')['href']='/fr/presse/dossier/changed-source-record';self.lookup[M._LIST_URL]=str(soup)
  with self.assertRaisesRegex(ValueError,'source list changed'):self.run_crawl(obj)
  self.assertEqual(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0],0);self.assertFalse(obj.delivery_exhausted);self.assertEqual(obj.delivery_source_failure,'invalid_response')
 def test_failed_response_and_bad_pagination_are_not_end(self):
  _,obj=self.crawler();self.lookup[M._LIST_URL]='<html><h1>Unavailable</h1></html>'
  with self.assertRaisesRegex(ValueError,'catalogue'):self.run_crawl(obj)
  self.assertFalse(obj.delivery_exhausted)
  raw=(F/'dossier-scoped-list.html').read_text();soup=BeautifulSoup(raw,'lxml');li=soup.new_tag('li',attrs={'class':'pager__item--next'});a=soup.new_tag('a',href=M._LIST_SCOPES['document']+'?page=1');li.append(a);soup.select_one('.view-id-press_release').append(li)
  with self.assertRaisesRegex(ValueError,'changed catalogue'):obj._parse_source_page(str(soup),'dossier',0)
 def test_save_then_failure_replay_preserves_committed_row(self):
  c,obj=self.crawler();save=obj._save_paper
  def fail_after_commit(p):save(p);raise RuntimeError('simulated failure before cursor advancement')
  with patch.object(obj,'_save_paper',side_effect=fail_after_commit):
   with self.assertRaises(RuntimeError):self.run_crawl(obj)
  before=dict(c.execute('SELECT * FROM documents').fetchone());self.assertIsNone(obj.delivery_pending_cursor)
  _,obj=self.crawler(conn=c);self.assertEqual(self.run_crawl(obj),1);rows=[dict(x) for x in c.execute('SELECT * FROM documents')];self.assertEqual(len(rows),2);self.assertEqual(rows[0],before)
 def test_zero_limit_and_cancel_before_request(self):
  _,obj=self.crawler();obj.delivery_should_cancel=lambda:True
  with patch.object(obj,'_curl',side_effect=AssertionError('unexpectedHTTP')):
   self.assertEqual(obj.crawl(limit=0),0)
   with self.assertRaises(CrawlCancelled):obj.crawl()
if __name__=='__main__':unittest.main()
