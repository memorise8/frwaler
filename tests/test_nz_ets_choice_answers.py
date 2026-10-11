import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/nz_ets_choice_answers'
SPEC = importlib.util.spec_from_file_location(
    'nz_ets_choices', ROOT / 'crawler/sites/custom/consult-environment-govt-nz-climate.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class NzEtsChoiceAnswerTests(unittest.TestCase):
    def parse(self, index, raw=None):
        source = json.loads((FIXTURES / 'sources.json').read_text())['sources'][index]
        crawler = object.__new__(MODULE.ConsultEnvironmentGovtNzClimateCrawler)
        with contextlib.redirect_stdout(io.StringIO()):
            return crawler._parse_respondent(
                raw if raw is not None else (FIXTURES / source['fixture_file']).read_text(),
                source['uid'], source['canonical_native_url'])

    def test_actual_anonymous_choice_only_submission_is_not_dropped(self):
        paper = self.parse(0)
        self.assertEqual(paper['external_id'], '804102122')
        self.assertEqual(paper['title'], 'Response 804102122')
        self.assertEqual(paper['abstract'],
                         'Q: 9. Which is your preferred option?\nA: Option 1: Status quo')
        self.assertNotIn('Option 2', paper['abstract'])
        self.assertEqual(paper['published_date'], '2025-08-19')

    def test_named_choice_submission_preserves_name_and_selected_label(self):
        paper = self.parse(1)
        self.assertEqual(paper['title'], 'Ruth Thomas')
        self.assertEqual(paper['authors'], 'Ruth Thomas')
        self.assertEqual(paper['abstract'],
                         'Q: 1. Submitter name\nA: Ruth Thomas\n\n'
                         'Q: 9. Which is your preferred option?\nA: Option 1: Status quo')

    def test_unchecked_visible_option_is_not_an_answer(self):
        raw = (FIXTURES / 'response-0.html').read_text()
        soup = BeautifulSoup(raw, 'lxml')
        for icon in soup.select('.cs-question-container .fa-check'):
            icon['class'] = [c for c in icon.get('class', []) if c != 'fa-check']
        self.assertIsNone(self.parse(0, str(soup)))


if __name__ == '__main__':
    unittest.main()
