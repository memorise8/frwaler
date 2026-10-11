import importlib.util,json,sqlite3,unittest
from pathlib import Path
from bs4 import BeautifulSoup
from crawler import db,db_libertree
from crawler.base_crawler import CrawlCancelled
from unittest.mock import patch
import contextlib,io
ROOT=Path(__file__).resolve().parents[1]
SPEC=importlib.util.spec_from_file_location('cancer_fr_revision',ROOT/'crawler/sites/custom/cancer-fr-presse.py')
M=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(M)
F=ROOT/'tests/fixtures/cancer_fr_revision_dates'

class CancerFrRevisionDateTests(unittest.TestCase):
    def setUp(self):
        self.obj=object.__new__(M.CancerFrPresseCrawler)
        self.old=json.loads((F/'expected-before.json').read_text())
    def papers(self):
        return [self.obj._parse_detail((F/(label+'.html')).read_text(),old['url']) for label,old in zip(['first','last'],self.old)]
    def test_actual_first_last_preserve_identity_body_and_revision_raw(self):
        for old,new in zip(self.old,self.papers()):
            ignored={'published_date','posted_date','metadata'}
            self.assertEqual({k:v for k,v in old.items() if k not in ignored},{k:v for k,v in new.items() if k not in ignored})
            self.assertIsNone(new['published_date']);self.assertIsNone(new['posted_date'])
            m=json.loads(new['metadata']);self.assertEqual(m['revision_date'],old['published_date'])
            self.assertIn('Mis à jour le',m['revision_date_raw']);self.assertEqual(m['revision_date_role'],'article_revision')
            self.assertEqual(m['revision_datetime_raw'],old['published_date'])
    def test_explicit_publication_is_independent_of_revision(self):
        s=BeautifulSoup((F/'first.html').read_text(),'lxml');s.select_one('div.fw-bold').insert(0,'Publié le 2 octobre 2026. ')
        p=self.obj._parse_detail(str(s),self.old[0]['url'])
        self.assertEqual(p['published_date'],'2026-10-02');self.assertEqual(json.loads(p['metadata'])['revision_date'],'2026-10-06')
        self.assertIsNone(p['posted_date'])
    def test_invalid_dates_remain_raw_and_missing_revision_not_invented(self):
        s=BeautifulSoup((F/'first.html').read_text(),'lxml');s.select_one('time')['datetime']='2026-02-31'
        s.select_one('div.fw-bold').insert(0,'Publié le 31 février 2026. ')
        p=self.obj._parse_detail(str(s),self.old[0]['url']);m=json.loads(p['metadata'])
        self.assertIsNone(p['published_date']);self.assertIsNone(m['revision_date']);self.assertEqual(m['publication_date_raw'],'31 février 2026');self.assertEqual(m['revision_datetime_raw'],'2026-02-31')
        s.select_one('p.article-section-date').decompose()
        self.assertIsNone(json.loads(self.obj._parse_detail(str(s),self.old[0]['url'])['metadata'])['revision_date_raw'])
    def test_actual_two_papers_new_repeat_both_owned_schemas(self):
        for kind in ['legacy','v2']:
            c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close)
            if kind=='legacy':db.init_db(c);db.register_site(c,self.obj.site_id,self.obj.site_id,self.obj.base_url)
            else:db_libertree.init_db(c);c.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',(self.obj.site_id,self.obj.site_id,self.obj.base_url));c.commit()
            obj=M.CancerFrPresseCrawler(c,delay=0)
            for p in self.papers():obj._save_paper(p);self.assertTrue(obj._last_save_created)
            rows=[dict(x) for x in c.execute('SELECT * FROM documents')]
            for p in self.papers():obj._save_paper(p);self.assertFalse(obj._last_save_created)
            self.assertEqual(rows,[dict(x) for x in c.execute('SELECT * FROM documents')]);self.assertEqual(len(rows),2)
            self.assertTrue(all(not x.get('published_date') and not x.get('posted_date') for x in rows))
    def test_actual_save_cancellation_and_replay_with_no_request_when_stopped(self):
        c=sqlite3.connect(':memory:');c.row_factory=sqlite3.Row;self.addCleanup(c.close)
        db.init_db(c);db.register_site(c,self.obj.site_id,self.obj.site_id,self.obj.base_url)
        obj=M.CancerFrPresseCrawler(c,delay=0)
        lookup={p['url']:(F/(name+'.html')).read_text() for name,p in zip(['first','last'],self.old)}
        lookup[M._LIST_BASE+'?'+M._LIST_QUERY]='scoped listing supplied by test'
        obj.delivery_should_cancel=lambda:c.execute('SELECT COUNT(*) FROM documents').fetchone()[0]>=1
        with patch.object(obj,'_curl',side_effect=lookup.__getitem__),patch.object(obj,'_parse_list_page',return_value=([p['url'] for p in self.old],1)),patch.object(M.time,'sleep'),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(CrawlCancelled):obj.crawl()
            first=dict(c.execute('SELECT * FROM documents').fetchone())
            obj.delivery_should_cancel=lambda:False
            self.assertEqual(obj.crawl(),1);self.assertEqual(obj.crawl(),0)
            self.assertEqual(first,dict(c.execute('SELECT * FROM documents ORDER BY id LIMIT 1').fetchone()))
        with patch.object(obj,'_curl',side_effect=AssertionError('unexpected request')):
            self.assertEqual(obj.crawl(limit=0),0)
            obj.delivery_should_cancel=lambda:True
            with self.assertRaises(CrawlCancelled):obj.crawl()
if __name__=='__main__':unittest.main()
