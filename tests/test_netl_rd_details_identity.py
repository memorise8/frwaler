"""Use the source Details action, even when the abstract cites other report IDs."""
import importlib.util
import re
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / 'fixtures/netl_rd_details_identity'


class NETLDetailsIdentity(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'netl_rd_identity', ROOT / 'crawler/sites/custom/netl-doe-gov-search-r-d-publicati.py')
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.native = object.__new__(self.module.NETLDoeRDPublicationsCrawler)

    def test_all64_actual_source_details_ids_preserve_distinct_documents(self):
        for filename, count in [('year2026.html', 6), ('year2025.html', 58)]:
            with self.subTest(filename=filename):
                raw = (FIXTURES / filename).read_text()
                soup = BeautifulSoup(raw, 'lxml')
                details = [a for a in soup.select('a[href]') if a.get_text(' ', strip=True) == 'Details']
                expected = [parse_qs(urlsplit(a['href']).query)['id'][0] for a in details]
                result = self.native._parse_list(raw)
                self.assertEqual(len(result), count)
                self.assertEqual([x['uuid'] for x in result], expected)
                self.assertEqual(len(set(expected)), count)
        self.assertEqual(result[17]['uuid'], 'b966302c-b274-46f9-88f9-8566434765eb')
        self.assertEqual(result[18]['uuid'], 'f0cc7b1d-840b-4e80-aaf9-a2f2a32862f8')
        self.assertEqual(result[55]['uuid'], 'c699a604-ded1-45cd-b1c0-247b1405aeeb')
        self.assertNotEqual(result[17]['uuid'], result[18]['uuid'])

    def test_description_reference_alone_does_not_create_a_result(self):
        raw = '<div id="panelResults"><p><strong>Unidentified result</strong></p><p>See /energy-analysis/details?id=5dcaf750-0620-4dde-8dba-fa42b79e333b</p></div>'
        self.assertEqual(self.native._parse_list(raw), [])

    def test_description_hyperlink_cannot_override_details_button(self):
        source_id = 'b966302c-b274-46f9-88f9-8566434765eb'
        cited_id = '5dcaf750-0620-4dde-8dba-fa42b79e333b'
        raw = f'<div id="panelResults"><p><strong>Toolkit</strong></p><p><a href="/energy-analysis/details?id={cited_id}">Related guidance</a></p><a class="btn" href="/energy-analysis/details?id={source_id}" target="_blank">Details</a></div>'
        self.assertEqual(self.native._parse_list(raw)[0]['uuid'], source_id)


if __name__ == '__main__':
    unittest.main()
