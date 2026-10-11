import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from crawler import db, db_libertree, libertree_adapter, storage

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'bka_legacy_identity', ROOT / 'crawler/sites/custom/bundeskanzleramt-gv-at-bundeskanzleramt.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
PAPERS = json.loads((ROOT / 'tests/fixtures/bka_year_pagination/collision-papers.json').read_text())['papers']
SID = MODULE._SITE_ID


class BkaLegacyIdentityTests(unittest.TestCase):
    def make_db(self, kind='legacy'):
        conn = sqlite3.connect(':memory:')
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        if kind == 'legacy':
            db.init_db(conn)
            db.register_site(conn, SID, SID, MODULE._BASE_URL)
        else:
            db_libertree.init_db(conn)
            conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',
                         (SID, SID, MODULE._BASE_URL))
            conn.commit()
        crawler = MODULE.BundeskanzleramtGvAtBundeskanzleramtCrawler(conn, delay=0)
        return conn, crawler

    def test_four_actual_documents_survive_legacy_storage_and_reverse_order_replay(self):
        mappings = []
        for papers in (PAPERS, list(reversed(PAPERS))):
            conn, crawler = self.make_db()
            for paper in papers:
                crawler._save_paper(copy.deepcopy(paper))
                self.assertTrue(crawler._last_save_created)
            before = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY id')]
            self.assertEqual(len(before), 4)
            self.assertEqual({r['meta_url'] for r in before}, {p['url'] for p in PAPERS})
            for paper in reversed(papers):
                crawler._save_paper(copy.deepcopy(paper))
                self.assertFalse(crawler._last_save_created)
            self.assertEqual(before, [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY id')])
            mappings.append({r['meta_url']: r['external_id'] for r in before})
        self.assertEqual(mappings[0], mappings[1])

    def test_existing_slug_row_id_summary_and_files_remain_attached_to_original_URL(self):
        for original in PAPERS:
            conn, crawler = self.make_db()
            row_id = db.upsert_document(conn, libertree_adapter.paper_to_document(copy.deepcopy(original)))
            with tempfile.TemporaryDirectory() as directory:
                pdf, text = Path(directory) / 'kept.pdf', Path(directory) / 'kept.txt'
                pdf.write_bytes(b'existing owned file sentinel')
                text.write_text('existing owned text sentinel')
                conn.execute('UPDATE documents SET pdf_path=?,txt_path=?,summary=? WHERE id=?',
                             (str(pdf), str(text), 'existing reviewed summary', row_id))
                conn.commit()
                with patch.object(storage, 'cleanup_doc_files') as cleanup:
                    for paper in PAPERS:
                        crawler._save_paper(copy.deepcopy(paper))
                    cleanup.assert_not_called()
                row = dict(conn.execute('SELECT * FROM documents WHERE id=?', (row_id,)).fetchone())
                self.assertEqual(row['meta_url'], original['url'])
                self.assertEqual(row['external_id'], original['external_id'])
                self.assertEqual(row['published_date'], original['published_date'])
                self.assertEqual(row['summary'], 'existing reviewed summary')
                self.assertEqual((row['pdf_path'], row['txt_path']), (str(pdf), str(text)))
                self.assertEqual(pdf.read_bytes(), b'existing owned file sentinel')
                self.assertEqual(text.read_text(), 'existing owned text sentinel')
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 4)

    def test_v2_preserves_existing_slug_numbers_and_four_distinct_URLs(self):
        conn, crawler = self.make_db('v2')
        for paper in PAPERS:
            crawler._save_paper(copy.deepcopy(paper))
        before = [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        self.assertEqual(len(before), 4)
        self.assertEqual({r['post_number'] for r in before}, {p['external_id'] for p in PAPERS})
        for paper in PAPERS:
            crawler._save_paper(copy.deepcopy(paper))
            self.assertFalse(crawler._last_save_created)
        self.assertEqual(before, [dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')])

    def test_existing_URL_with_missing_key_is_preserved_for_explicit_repair(self):
        conn, crawler = self.make_db()
        conn.execute('INSERT INTO documents(site_id,meta_url,title) VALUES(?,?,?)',
                     (SID, PAPERS[0]['url'], 'existing row with unknown identity'))
        conn.commit()
        before = [dict(r) for r in conn.execute('SELECT * FROM documents')]
        with self.assertRaisesRegex(ValueError, 'Ambiguous existing legacy news identity'):
            crawler._save_paper(copy.deepcopy(PAPERS[0]))
        self.assertEqual(before, [dict(r) for r in conn.execute('SELECT * FROM documents')])


if __name__ == '__main__':
    unittest.main()
