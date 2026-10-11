import json
import sqlite3
import unittest
from unittest.mock import Mock

from crawler.incremental import Entry
from crawler.incremental_adapters import DOAJAdapter, HALAdapter, NTRSAdapter


class AdapterTests(unittest.TestCase):
    def test_stable_identity_lookup_uses_id_not_null_url(self):
        c=Mock();c.site_id='test';c._conn_is_libertree.return_value=True
        c._conn=sqlite3.connect(':memory:')
        c._conn.execute('CREATE TABLE documents(site_id TEXT,post_number TEXT,meta_url TEXT)')
        c._conn.execute("INSERT INTO documents VALUES('test','123','https://source/123')")
        self.assertTrue(NTRSAdapter(c).exists(Entry('123',{})))
        c._conn.close()

    def test_doaj_tied_timestamp_keeps_all_ids_and_never_uses_deep_pages(self):
        c=Mock();c.site_id='doaj-org-search'
        a=DOAJAdapter(c);calls=[]
        batches=[{'results':[{'id':'a','created_date':'2026-01-01T00:00:00Z'}, {'id':'b','created_date':'2026-01-01T00:00:00Z'}],'total':4},
                 {'results':[{'id':'c','created_date':'2026-01-01T00:00:00Z'}],'total':2},
                 {'results':[{'id':'d','created_date':'2025-01-01T00:00:00Z'}],'total':1}]
        def fetch(url,params):calls.append((url,params));return batches.pop(0)
        a.get=fetch
        p=a.fetch(None,'native');self.assertEqual(p.next_cursor['ties'],['a','b'])
        p=a.fetch(p.next_cursor,'native');self.assertEqual(p.next_cursor['ties'],['a','b','c'])
        p=a.fetch(p.next_cursor,'native');self.assertTrue(p.exhausted)
        self.assertEqual(p.next_cursor['ties'],['d'])
        self.assertTrue(all(call[1]['page']==1 for call in calls))
        self.assertIn('a%20OR%20b%20OR%20c',calls[2][0])

    def test_hal_uses_date_and_unique_id_cursor_not_numeric_docid_assumption(self):
        c=Mock();c.site_id='cea-hal-science-cnrgh';a=HALAdapter(c);calls=[]
        responses=[{'response':{'docs':[{'docid':'999999','submittedDate_tdate':'2014-06-04T11:11:43Z'},
                                      {'docid':'5792865','submittedDate_tdate':'2026-10-11T16:51:11Z'}], 'numFound':2},'nextCursorMark':'opaque'},
                   {'response':{'docs':[], 'numFound':2},'nextCursorMark':'opaque'}]
        def get(url,params):calls.append(dict(params));return responses.pop(0)
        a.get=get
        p=a.fetch(None,'native');self.assertFalse(p.exhausted)
        p=a.fetch(p.next_cursor,'native');self.assertTrue(p.exhausted)
        self.assertEqual(calls[0]['sort'],'submittedDate_tdate asc,docid asc')
        self.assertEqual(calls[1]['cursorMark'],'opaque')
        self.assertFalse(a.numeric_ids)

    def test_hal_preserves_legacy_hal_id_in_document_storage(self):
        c=Mock();c.site_id='cea-hal-science-cnrgh';a=HALAdapter(c)
        self.assertEqual(a.materialize({'docid':'123','halId_s':'hal-00000123','title_s':['Title']})['external_id'],'hal-00000123')
        c.site_id='inria-hal-science-search';a=HALAdapter(c)
        self.assertEqual(a.materialize({'docid':'123','halId_s':'hal-00000123','title_s':['Title']})['external_id'],'123')

    def test_offset_drift_cannot_skip_an_unsaved_item(self):
        c=Mock();c.site_id='ntrs';a=NTRSAdapter(c)
        a.read=lambda p:([{'id':'1','created':'2026-01-01'}],2)
        first=a.fetch(None,'native')
        a.read=lambda p:([{'id':'2','created':'2026-01-02'}],2)
        with self.assertRaisesRegex(ValueError,'drift'):a.fetch(first.next_cursor,'native')
        self.assertFalse(a.can_read_newest)

if __name__=='__main__':unittest.main()
