import importlib.util,json,sqlite3,unittest,contextlib,io,tempfile
from pathlib import Path
from unittest.mock import patch
from bs4 import BeautifulSoup
from crawler import db,db_libertree,storage
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cae_url_encoding';PRESS=ROOT/'tests/fixtures/cae_pdf_only'
SP=importlib.util.spec_from_file_location('cae_url_encoding',ROOT/'crawler/sites/custom/cae-eco-fr-presse-cae-0.py');M=importlib.util.module_from_spec(SP);SP.loader.exec_module(M)
class CaeUrlEncodingTests(unittest.TestCase):
 def setUp(self):self.before=json.loads((F/'expected-before.json').read_text())
 def crawler(self,kind):
  c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close);sid=M.CaeEcoFrPresseCae0Crawler.site_id
  if kind=='legacy':db.init_db(c);db.register_site(c,sid,sid,M.BASE)
  else:db_libertree.init_db(c);c.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(sid,sid,M.BASE));c.commit()
  return c,M.CaeEcoFrPresseCae0Crawler(c,delay=0)
 def capture(self,obj):
  s=BeautifulSoup((PRESS/'list.html').read_text(),'lxml')
  for card in list(s.select('article.portfolio-item')):
   if M.BASE+'/'+card.select_one('h3 a')['href']!=self.before['url']:card.decompose()
  lookup={M.BASE+'/Presse-CAE-0':str(s),M.BASE+'/Presse-CAE-1':(PRESS/'empty-list.html').read_text(),self.before['url']:(F/'detail.html').read_text()};papers=[];save=obj._save_paper
  def record(p):papers.append(p);return save(p)
  with patch.object(M,'_curl',side_effect=lookup.__getitem__),patch.object(M.time,'sleep'),patch.object(obj,'_save_paper',side_effect=record),contextlib.redirect_stdout(io.StringIO()):count=obj.crawl()
  return count,papers
 def test_actual_url_space_encoded_without_changing_ID_filename_or_content(self):
  c,obj=self.crawler('legacy');count,papers=self.capture(obj);self.assertEqual(count,1);p=papers[0]
  self.assertEqual(p['pdf_url'],self.before['pdf_url'].replace(' ','%20'))
  self.assertEqual({k:v for k,v in p.items() if k not in ('pdf_url','metadata')},{k:v for k,v in self.before.items() if k not in ('pdf_url','metadata')})
  meta=json.loads(p['metadata']);self.assertEqual(meta['primary_pdf_url_raw'],self.before['pdf_url']);self.assertIn(self.before['pdf_url'],meta['source_pdf_urls_raw'])
 def test_existing_escapes_and_ambiguous_source_query_are_preserved(self):
  encoded=self.before['pdf_url'].replace(' ','%20');self.assertEqual(M._pdf_request_url(encoded),encoded)
  source=M.BASE+'/static/pdf/communique?_de_presse-note060.pdf';self.assertEqual(M._pdf_request_url(source),source)
 def test_actual_native_new_repeat_both_schemas(self):
  for kind in ['legacy','v2']:
   c,obj=self.crawler(kind);self.assertEqual(self.capture(obj)[0],1);rows=[dict(x) for x in c.execute('SELECT * FROM documents')]
   self.assertEqual(self.capture(obj)[0],0);self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')])
 def test_existing_raw_URL_row_and_files_are_held_unchanged(self):
  c,obj=self.crawler('legacy');obj._save_paper(dict(self.before,summary='existing summary'))
  with tempfile.TemporaryDirectory() as directory:
   f=Path(directory)/'owned.pdf';text=Path(directory)/'owned.txt';f.write_bytes(b'preserved attachment');text.write_text('preserved text')
   c.execute('UPDATE documents SET pdf_path=?,txt_path=?',(str(f),str(text)));c.commit();before=dict(c.execute('SELECT * FROM documents').fetchone())
   p=dict(self.before,pdf_url=M._pdf_request_url(self.before['pdf_url']))
   with patch.object(storage,'cleanup_doc_files',side_effect=AssertionError('must preserve files')):
    with self.assertRaisesRegex(ValueError,'preserve files'):obj._save_paper(p)
   self.assertEqual(before,dict(c.execute('SELECT * FROM documents').fetchone()));self.assertEqual(f.read_bytes(),b'preserved attachment');self.assertEqual(text.read_text(),'preserved text')
if __name__=='__main__':unittest.main()
