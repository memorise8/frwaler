import contextlib,importlib.util,io,json,sqlite3,unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler import db,db_libertree,storage
from crawler.base_crawler import CrawlCancelled
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cerema_pdf_only';D=ROOT/'tests/fixtures/cerema_original_scopes';S=importlib.util.spec_from_file_location('cerema_pdf_only',ROOT/'crawler/sites/custom/cerema-fr-fr.py');M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
class CeremaPdfOnlyTests(unittest.TestCase):
 def setUp(self):
  self.old=json.loads((F/'before-paper.json').read_text());self.doc=json.loads((D/'expected-before.json').read_text())[1];self.lookup={M._LIST_SCOPES['dossier']:(F/'scoped-list.html').read_text(),self.old['url']:(F/'detail.html').read_text(),M._LIST_SCOPES['document']:(D/'document-scoped-list.html').read_text(),self.doc['url']:(D/'document-detail.html').read_text()}
 def crawler(self,kind='legacy',conn=None):
  if conn is None:
   conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
   if kind=='legacy':db.init_db(conn);db.register_site(conn,M._SITE_ID,M._SITE_ID,M._BASE_URL)
   else:db_libertree.init_db(conn);conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(M._SITE_ID,M._SITE_ID,M._BASE_URL));conn.commit()
  return conn,M.CeremaFrFrCrawler(conn,delay=0)
 def run_crawl(self,obj):
  with patch.object(obj,'_curl',side_effect=lambda u,**kw:self.lookup[u]),patch.object(M.time,'sleep'),patch.object(storage,'cleanup_doc_files',side_effect=AssertionError('unexpected cleanup')),contextlib.redirect_stdout(io.StringIO()):return obj.crawl()
 def test_actual_PDF_only_fields_and_empty_abstract_are_preserved(self):
  _,obj=self.crawler();items,_=obj._parse_source_page(self.lookup[M._LIST_URL],'dossier',0);paper=obj._parse_detail(self.lookup[self.old['url']],items[0]);self.assertEqual({k:v for k,v in paper.items() if k!='metadata'},{k:v for k,v in self.old.items() if k!='metadata'});self.assertEqual(paper['abstract'],'');meta=json.loads(paper['metadata']);self.assertEqual(meta['source_abstract_state'],'empty');self.assertEqual(meta['source_fulltext_file_url'],self.old['pdf_url'])
 def test_actual_PDF_only_cancel_resume_crosses_to_second_scope_both_DBs(self):
  for kind in ['legacy','v2']:
   c,obj=self.crawler(kind);obj.delivery_should_cancel=lambda:c.execute('SELECT COUNT(*) FROM documents').fetchone()[0]>=1
   with self.assertRaises(CrawlCancelled):self.run_crawl(obj)
   before=dict(c.execute('SELECT * FROM documents').fetchone());self.assertEqual(before['abstract'],'');self.assertEqual(before['pdf_url'],self.old['pdf_url']);cursor=obj.delivery_pending_cursor;self.assertEqual(cursor['scope'],'document');_,obj=self.crawler(kind,c);obj.delivery_cursor=cursor;self.assertEqual(self.run_crawl(obj),1);self.assertTrue(obj.delivery_exhausted);rows=[dict(x) for x in c.execute('SELECT * FROM documents')];self.assertEqual(rows[0],before);_,again=self.crawler(kind,c);self.assertEqual(self.run_crawl(again),0);self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')])
 def test_empty_body_without_source_file_still_holds_cursor(self):
  c,obj=self.crawler();soup=BeautifulSoup(self.lookup[self.old['url']],'lxml')
  for a in soup.select('a[href]'):
   if '.pdf' in a['href'].lower():a['href']='#'
  self.lookup[self.old['url']]=str(soup)
  with self.assertRaisesRegex(ValueError,'abstract requires review'):self.run_crawl(obj)
  self.assertFalse(obj.delivery_exhausted);self.assertIsNone(obj.delivery_pending_cursor);self.assertEqual(c.execute('SELECT COUNT(*) FROM documents').fetchone()[0],0)
 def test_actual_short_resource_page_preserves_body_and_three_official_links(self):
  g=ROOT/'tests/fixtures/cerema_resource_links';before=json.loads((g/'before-paper.json').read_text());_,obj=self.crawler();items,_=obj._parse_source_page((g/'scoped-list.html').read_text(),'document',0);p=obj._parse_detail((g/'detail.html').read_text(),items[0]);self.assertEqual({k:v for k,v in p.items() if k!='metadata'},{k:v for k,v in before.items() if k!='metadata'});self.assertFalse(p['pdf_url']);self.assertEqual(len(p['abstract']),77);links=json.loads(p['metadata'])['source_resource_links'];self.assertEqual([x['url'] for x in links],['https://www.legifrance.gouv.fr/jorf/id/JORFTEXT000045924838','https://www.legifrance.gouv.fr/affichTexte.do?cidTexte=JORFTEXT000027469717&dateTexte=&categorieLien=id','https://www.legifrance.gouv.fr/affichTexte.do?cidTexte=JORFTEXT000028398714&fastPos=1&fastReqId=152283380&categorieLien=id&oldAction=rech'])
 def test_actual_PDF_then_resource_page_resume_both_schemas(self):
  g=ROOT/'tests/fixtures/cerema_resource_links';resource=json.loads((g/'before-paper.json').read_text());self.lookup[M._LIST_SCOPES['document']]=(g/'scoped-list.html').read_text();self.lookup[resource['url']]=(g/'detail.html').read_text()
  for kind in ['legacy','v2']:
   c,obj=self.crawler(kind);obj.delivery_should_cancel=lambda:c.execute('SELECT COUNT(*) FROM documents').fetchone()[0]>=1
   with self.assertRaises(CrawlCancelled):self.run_crawl(obj)
   first=dict(c.execute('SELECT * FROM documents').fetchone());cursor=obj.delivery_pending_cursor;_,obj=self.crawler(kind,c);obj.delivery_cursor=cursor;self.assertEqual(self.run_crawl(obj),1);self.assertTrue(obj.delivery_exhausted);rows=[dict(x) for x in c.execute('SELECT * FROM documents')];self.assertEqual(rows[0],first);self.assertEqual(rows[1]['abstract'],resource['abstract']);self.assertEqual(rows[1]['meta_url'],resource['url']);self.assertFalse(rows[1]['pdf_url']);_,again=self.crawler(kind,c);self.assertEqual(self.run_crawl(again),0);self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')])
if __name__=='__main__':unittest.main()
