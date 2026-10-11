import hashlib
import importlib.util
import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler import db, db_libertree

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/cmi_embedded_script'
SPEC = importlib.util.spec_from_file_location(
    'cmi_embedded_script', ROOT / 'crawler/sites/custom/cmi-no-publications.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class CmiEmbeddedScriptTests(unittest.TestCase):
    def paper(self, backend='html5lib'):
        raw = (FIXTURES / 'article-6280.html').read_text()
        expected = json.loads((FIXTURES / 'expected.json').read_text())
        obj = object.__new__(MODULE.CmiNoPublicationsCrawler)
        with patch.object(MODULE, '_curl_get', return_value=raw), patch.object(
                MODULE, '_make_soup', side_effect=lambda html: BeautifulSoup(html, backend)):
            return obj._parse_detail(expected['nonabstract_fields']['url'], '6280')

    def test_actual_embedded_publisher_scripts_excluded_all_backends(self):
        expected = json.loads((FIXTURES / 'expected.json').read_text())
        for backend in ['html5lib', 'lxml', 'html.parser']:
            with self.subTest(backend=backend):
                paper = self.paper(backend)
                self.assertEqual({k: v for k, v in paper.items() if k != 'abstract'},
                                 expected['nonabstract_fields'])
                self.assertEqual(len(paper['abstract']), expected['expected_abstract_characters'])
                self.assertEqual(hashlib.sha256(paper['abstract'].encode()).hexdigest(),
                                 expected['expected_abstract_sha256'])
                self.assertNotIn('var servletLocation', paper['abstract'])
                self.assertIn('Progressive contextualization', paper['abstract'])

    def test_actual_clean_body_persisted_and_repeat_preserves_both_schemas(self):
        paper = self.paper()
        cls = MODULE.CmiNoPublicationsCrawler
        for kind in ['legacy', 'v2']:
            with self.subTest(schema=kind):
                conn = sqlite3.connect(':memory:')
                conn.row_factory = sqlite3.Row
                self.addCleanup(conn.close)
                if kind == 'legacy':
                    db.init_db(conn)
                    db.register_site(conn, cls.site_id, cls.site_id, cls.base_url)
                else:
                    db_libertree.init_db(conn)
                    conn.execute('INSERT INTO sites(site_id,site_name,site_url) VALUES(?,?,?)',
                                 (cls.site_id, cls.site_id, cls.base_url))
                    conn.commit()
                obj = cls(conn, delay=0)
                obj._save_paper(paper)
                self.assertTrue(obj._last_save_created)
                before = dict(conn.execute('SELECT * FROM documents').fetchone())
                self.assertEqual(before['abstract'], paper['abstract'])
                obj._save_paper(paper)
                self.assertFalse(obj._last_save_created)
                self.assertEqual([before], [dict(x) for x in conn.execute('SELECT * FROM documents')])


if __name__ == '__main__':
    unittest.main()
