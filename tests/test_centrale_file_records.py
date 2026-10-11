import contextlib,copy,importlib.util,io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler import db,db_libertree,libertree_adapter,storage
from crawler.base_crawler import CrawlCancelled
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/centrale_file_records';S=importlib.util.spec_from_file_location('centrale_files',ROOT/'crawler/sites/custom/centralesupelec-fr-presse.py');M=importlib.util.module_from_spec(S);S.loader.exec_module(M);SID=M.CentraleSupelecPresseCrawler.site_id;SOURCES=json.loads((F/'sources.json').read_text());BEFORE=json.loads((F/'before-first-paper.json').read_text())
class CentraleFileRecordTests(unittest.TestCase):
 def setUp(self):
  cls=M.CentraleSupelecPresseCrawler;self.lookup={cls._LIST_URL+'?page=0':((F/'scoped-three-list.html').read_text(),cls._LIST_URL),cls._LIST_URL+'?page=1':((F/'empty-boundary.html').read_text(),cls._LIST_URL+'?page=1')}
  for row,src in zip(SOURCES['records'],SOURCES['sources'][:3]):self.lookup[cls.base_url+'/node/'+row['node_id']]=((F/src['fixture_file']).read_text(),src['url'])
 def make_db(self,kind='legacy',conn=None):
  if conn is None:
   conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
   if kind=='legacy':db.init_db(conn);db.register_site(conn,SID,SID,M.CentraleSupelecPresseCrawler.base_url)
   else:db_libertree.init_db(conn);conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(SID,SID,M.CentraleSupelecPresseCrawler.base_url));conn.commit()
  return conn,M.CentraleSupelecPresseCrawler(conn,delay=0)
 def run_crawl(self,obj,**kw):
  with patch.object(obj,'_curl_get',side_effect=lambda u,**kwargs:self.lookup[u]),patch.object(M.time,'sleep'),patch.object(storage,'cleanup_doc_files',side_effect=AssertionError('unexpected cleanup')),contextlib.redirect_stdout(io.StringIO()):return obj.crawl(**kw)
 def rows(self,c):return [dict(x) for x in c.execute('SELECT * FROM documents')]
 def test_real_three_records_preserve_first_and_office_URLs_without_inventing_abstracts(self):
  c,obj=self.make_db();papers=[];save=obj._save_paper
  def record(p):papers.append(copy.deepcopy(p));return save(p)
  with patch.object(obj,'_save_paper',side_effect=record):self.assertEqual(self.run_crawl(obj),3)
  self.assertEqual(papers[0],BEFORE)
  for p,src in zip(papers[1:],SOURCES['records'][1:]):
   self.assertEqual(p['external_id'],src['node_id']);self.assertEqual(p['post_number'],src['node_id']);self.assertEqual(p['title'],src['native']['title']);self.assertEqual(p['published_date'],src['native']['published_date']);self.assertEqual(p['pdf_url'],src['url']);self.assertEqual(p['abstract'],'');self.assertEqual(p['original_filename'],src['url'].rsplit('/',1)[-1]);meta=json.loads(p['metadata']);self.assertEqual(meta['source_abstract_state'],'empty');self.assertEqual(meta['source_file_type'],src['url'].rsplit('.',1)[-1]);self.assertTrue(meta['source_file_conversion_pending'])
 def test_cancel_limit_resume_repeat_preserves_both_schemas(self):
  for kind in ['legacy','v2']:
   c,obj=self.make_db(kind);obj.delivery_should_cancel=lambda:len(self.rows(c))>=1
   with self.assertRaises(CrawlCancelled):self.run_crawl(obj)
   first=self.rows(c);self.assertEqual(len(first),1);_,obj=self.make_db(kind,c);self.assertEqual(self.run_crawl(obj,limit=1),1);self.assertEqual(len(self.rows(c)),2);self.assertEqual(self.rows(c)[0],first[0]);_,obj=self.make_db(kind,c);self.assertEqual(self.run_crawl(obj),1);before=self.rows(c);self.assertEqual(len(before),3);_,obj=self.make_db(kind,c);self.assertEqual(self.run_crawl(obj),0);self.assertEqual(before,self.rows(c));self.assertEqual({x['pdf_url'] for x in before},{x['url'] for x in SOURCES['records']})
 def test_existing_first_row_summary_and_owned_files_preserved(self):
  c,obj=self.make_db();row_id=db.upsert_document(c,libertree_adapter.paper_to_document(copy.deepcopy(BEFORE)))
  with tempfile.TemporaryDirectory() as td:
   pdf,txt=Path(td)/'kept.pdf',Path(td)/'kept.txt';pdf.write_bytes(b'owned existing file sentinel');txt.write_text('owned existing text sentinel');c.execute('UPDATE documents SET pdf_path=?,txt_path=?,summary=? WHERE id=?',(str(pdf),str(txt),'existing summary',row_id));c.commit();before=self.rows(c)[0];self.assertEqual(self.run_crawl(obj),2);self.assertEqual(before,self.rows(c)[0]);self.assertEqual(pdf.read_bytes(),b'owned existing file sentinel');self.assertEqual(txt.read_text(),'owned existing text sentinel')
 def test_actual_first_and_empty_pages_exclude_catalogue_landing_article(self):
  _,obj=self.make_db();items=obj._parse_list_page((F/'first-list.html').read_text());self.assertEqual(len(items),10);self.assertNotIn('34538',{x['node_id'] for x in items});self.assertEqual(obj._parse_list_page((F/'empty-boundary.html').read_text()),[])
 def test_cancellation_before_fetch_after_list_and_after_detail(self):
  for stop in ['before','list','detail']:
   c,obj=self.make_db();cancel=[stop=='before'];obj.delivery_should_cancel=lambda:cancel[0];calls=[]
   def fetch(url,**kwargs):
    calls.append(url)
    if (stop=='list' and '?page=' in url) or (stop=='detail' and '/node/' in url):cancel[0]=True
    return self.lookup[url]
   with patch.object(obj,'_curl_get',side_effect=fetch),patch.object(M.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
    with self.assertRaises(CrawlCancelled):obj.crawl()
   self.assertEqual(self.rows(c),[]);self.assertEqual(len(calls),{'before':0,'list':1,'detail':2}[stop])
 def test_zero_limit_and_nonfile_empty_record_are_not_saved(self):
  c,obj=self.make_db()
  with patch.object(obj,'_curl_get',side_effect=AssertionError('unexpected request')):self.assertEqual(obj.crawl(limit=0),0)
  soup=BeautifulSoup((F/'scoped-three-list.html').read_text(),'lxml')
  for a in soup.select('.wrap-teaser-content a.h5[href]'):
   if a['href'].endswith(('.docx','.xlsx')):a['href']='/presse/metadata-only'
  self.lookup[obj._LIST_URL+'?page=0']=(str(soup),obj._LIST_URL);self.assertEqual(self.run_crawl(obj),1);self.assertEqual(len(self.rows(c)),1)
if __name__=='__main__':unittest.main()
