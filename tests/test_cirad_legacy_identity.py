import contextlib,copy,importlib.util,io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from crawler import db,db_libertree,libertree_adapter,storage
from crawler.base_crawler import CrawlCancelled
ROOT=Path(__file__).resolve().parents[1];F=ROOT/'tests/fixtures/cirad_identity'
S=importlib.util.spec_from_file_location('cirad_identity',ROOT/'crawler/sites/custom/cirad-fr-espace-presse.py');M=importlib.util.module_from_spec(S);S.loader.exec_module(M)
PAPERS=json.loads((F/'papers.json').read_text());SOURCES=json.loads((F/'sources.json').read_text())['sources'];SID=M.CiradFrEspacePresseCrawler.site_id
class CiradLegacyIdentityTests(unittest.TestCase):
 def make_db(self,kind='legacy',conn=None):
  if conn is None:
   conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row;self.addCleanup(conn.close)
   if kind=='legacy':db.init_db(conn);db.register_site(conn,SID,SID,M.CiradFrEspacePresseCrawler.base_url)
   else:db_libertree.init_db(conn);conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(SID,SID,M.CiradFrEspacePresseCrawler.base_url));conn.commit()
  return conn,M.CiradFrEspacePresseCrawler(conn,delay=0)
 def detail(self,obj,index):
  src=SOURCES[index]
  with patch.object(obj,'_fetch',return_value=(F/src['fixture_file']).read_text()),contextlib.redirect_stdout(io.StringIO()):return obj._process_detail(**src['candidate'])
 def rows(self,c):return [dict(x) for x in c.execute('SELECT * FROM documents')]
 def test_actual_two_detail_papers_unchanged(self):
  _,obj=self.make_db();papers=[]
  def save(p):papers.append(p);obj._last_save_created=True
  with patch.object(obj,'_save_paper',side_effect=save):
   for i in range(2):self.detail(obj,i)
  self.assertEqual(papers,PAPERS)
 def test_actual_collision_both_orders_both_schemas_replay_preserves_rows(self):
  for kind in ['legacy','v2']:
   mappings=[]
   for order in [(0,1),(1,0)]:
    c,obj=self.make_db(kind)
    for i in order:self.assertTrue(self.detail(obj,i))
    before=self.rows(c);self.assertEqual(len(before),2);self.assertEqual({x['meta_url'] for x in before},{p['url'] for p in PAPERS})
    for i in reversed(order):self.assertFalse(self.detail(obj,i))
    self.assertEqual(before,self.rows(c));key='external_id' if kind=='legacy' else 'post_number';mappings.append({x['meta_url']:x[key] for x in before})
    if kind=='v2':self.assertEqual({x[key] for x in before},{'immunite-des-plantes'})
   self.assertEqual(mappings[0],mappings[1])
 def test_existing_slug_row_and_owned_file_references_remain_attached(self):
  for original in PAPERS:
   c,obj=self.make_db();row_id=db.upsert_document(c,libertree_adapter.paper_to_document(copy.deepcopy(original)))
   with tempfile.TemporaryDirectory() as td:
    pdf,txt=Path(td)/'kept.pdf',Path(td)/'kept.txt';pdf.write_bytes(b'owned file reference sentinel');txt.write_text('owned text reference sentinel')
    c.execute('UPDATE documents SET pdf_path=?,txt_path=?,summary=? WHERE id=?',(str(pdf),str(txt),'existing reviewed summary',row_id));c.commit();before=dict(c.execute('SELECT * FROM documents WHERE id=?',(row_id,)).fetchone())
    with patch.object(storage,'cleanup_doc_files',side_effect=AssertionError('unexpected file cleanup')):
     for i in range(2):self.detail(obj,i)
    self.assertEqual(before,dict(c.execute('SELECT * FROM documents WHERE id=?',(row_id,)).fetchone()));self.assertEqual(len(self.rows(c)),2);self.assertEqual(pdf.read_bytes(),b'owned file reference sentinel');self.assertEqual(txt.read_text(),'owned text reference sentinel')
 def test_scoped_actual_cards_cancel_reopen_finish_repeat_both_schemas(self):
  for kind in ['legacy','v2']:
   c,obj=self.make_db(kind);lookup={obj.LIST_URL:(F/'two-original-cards-scoped.html').read_text(),**{s['url']:(F/s['fixture_file']).read_text() for s in SOURCES}}
   def run(o):
    with patch.object(o,'_fetch',side_effect=lambda u,**kw:lookup[u]),patch.object(M.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):return o.crawl()
   obj.delivery_should_cancel=lambda:len(self.rows(c))>=1
   with self.assertRaises(CrawlCancelled):run(obj)
   first=self.rows(c);self.assertEqual(len(first),1);_,obj=self.make_db(kind,c);self.assertEqual(run(obj),1);self.assertEqual(self.rows(c)[0],first[0]);before=self.rows(c);_,obj=self.make_db(kind,c);self.assertEqual(run(obj),0);self.assertEqual(before,self.rows(c))
 def test_existing_unknown_identity_requires_explicit_repair(self):
  c,obj=self.make_db();c.execute('INSERT INTO documents(site_id,meta_url,title) VALUES(?,?,?)',(SID,PAPERS[0]['url'],'existing unknown key'));c.commit();before=self.rows(c)
  with self.assertRaisesRegex(ValueError,'Ambiguous existing CIRAD'):self.detail(obj,0)
  self.assertEqual(before,self.rows(c))
 def test_occupied_qualified_key_fails_without_overwriting(self):
  c,obj=self.make_db();p=copy.deepcopy(PAPERS[1]);p['external_id']='press:2023/immunite-des-plantes';db.upsert_document(c,libertree_adapter.paper_to_document(p));before=self.rows(c)
  with self.assertRaisesRegex(ValueError,'Conflicting existing CIRAD'):self.detail(obj,0)
  self.assertEqual(before,self.rows(c))
if __name__=='__main__':unittest.main()
