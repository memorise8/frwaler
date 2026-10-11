import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('bmg_revision_dates', ROOT / 'crawler/sites/custom/bundesgesundheitsministerium-de-service.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURE = ROOT / 'tests/fixtures/bmg_revision_dates'


class BmgRevisionDateTests(unittest.TestCase):
    def setUp(self):
        self.crawler = object.__new__(MODULE.BundesgesundheitsministeriumDeServiceCrawler)
        self.items = self.crawler._parse_list_page((FIXTURE / 'health-list.html').read_text())

    def test_actual_list_preserves_all_non_date_fields(self):
        before = json.loads((FIXTURE / 'expected-before-list.json').read_text())
        self.assertEqual(len(before), len(self.items))
        for old, new in zip(before, self.items):
            self.assertEqual({k:v for k,v in old.items() if k != 'published_date'},
                             {k:v for k,v in new.items() if k not in {'published_date', 'stand_date_raw'}})
            self.assertEqual(new['published_date'], '')
        self.assertEqual(self.items[0]['stand_date_raw'], 'Stand: Juni 2026')

    def test_actual_detail_retains_revision_month_without_fabricating_publication(self):
        before = json.loads((FIXTURE / 'expected-before-detail.json').read_text())
        after = self.crawler._parse_detail((FIXTURE / 'im-dialog-17.html').read_text(), self.items[0])
        self.assertEqual(before['published_date'], '2026-06-01')
        self.assertEqual(after['published_date'], '')
        self.assertEqual(after['listed_date'], '')
        excluded = {'published_date', 'listed_date', 'metadata'}
        self.assertEqual({k:v for k,v in before.items() if k not in excluded},
                         {k:v for k,v in after.items() if k not in excluded})
        metadata = json.loads(after['metadata'])
        evidence = metadata.pop('date_evidence')
        self.assertEqual(metadata, json.loads(before['metadata']))
        self.assertEqual(evidence, [{'raw':'Stand: Juni 2026','value':'2026-06','precision':'month','role':'revision','source':'Stand'}])

    def test_unknown_revision_is_preserved_without_promotion(self):
        raw = (FIXTURE / 'im-dialog-17.html').read_text().replace('Juni 2026', 'Sommer 2026')
        detail = self.crawler._parse_detail(raw, self.items[0])
        evidence = json.loads(detail['metadata'])['date_evidence'][0]
        self.assertEqual(evidence['raw'], 'Stand: Sommer 2026')
        self.assertIsNone(evidence['value'])
        self.assertEqual(evidence['precision'], 'unknown')
        self.assertEqual(detail['published_date'], '')

    def test_month_parser_does_not_add_day(self):
        self.assertEqual(MODULE._parse_stand_date('Stand: März 2025'), '2025-03')
        self.assertEqual(MODULE._parse_stand_date('Stand: unbekannt'), '')


if __name__ == '__main__':
    unittest.main()
