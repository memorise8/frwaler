import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'bra_legacy_english', ROOT / 'crawler/sites/custom/bra-se-english.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = ROOT / 'tests/fixtures/bra_legacy_english'


class BraLegacyEnglishTests(unittest.TestCase):
    def test_all_ten_actual_catalogue_memberships_survive_parsing(self):
        raw = (FIXTURE / 'source-page-offset290.html').read_text()
        expected = json.loads((FIXTURE / 'sources.json').read_text())
        items = MODULE._parse_list_items(raw)
        self.assertEqual(
            [{'url': item['url'], 'title': item['title']} for item in items],
            expected['expected_memberships'],
        )
        legacy = next(item for item in items if '/brainenglish/' in item['url'])
        self.assertEqual(legacy['title'], 'The sanction system for young offenders')
        self.assertEqual(legacy['date'], '2000-08-09')
        self.assertIn('Act (1964:167)', legacy['abstract'])

    def test_accepting_legacy_english_does_not_accept_other_namespaces(self):
        original = (FIXTURE / 'source-page-offset290.html').read_text()
        for prefix in ('/publikationer/', '/brainenglish/statistics/', '//example.org/'):
            with self.subTest(prefix=prefix):
                changed = original.replace('/english/publications/', prefix)
                changed = changed.replace('/brainenglish/publications/', prefix)
                self.assertEqual(MODULE._parse_list_items(changed), [])


if __name__ == '__main__':
    unittest.main()
