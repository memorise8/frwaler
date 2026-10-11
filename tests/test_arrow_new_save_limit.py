import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    'arrow_new_save_limit', ROOT / 'crawler/sites/custom/arrow-tudublin-ie-creaart.py'
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
FIXTURES = ROOT / 'tests/fixtures/arrow_new_saves'
SOURCES = json.loads((FIXTURES / 'sources.json').read_text())


def replay(existing=(), limit=None, repeat_first=False):
    crawler = object.__new__(MODULE.ArrowTudublinIeCreaartCrawler)
    requests, attempted, created, failures = [], [], [], []
    known = set(existing)

    def fetch(url):
        index = 0 if repeat_first else len(requests)
        requests.append(url)
        if index > 2 or len(requests) > 3:
            raise AssertionError('Unexpected extra OAI request')
        return (FIXTURES / f'page-{index}.xml').read_bytes()

    def save(paper):
        key = paper['external_id']
        attempted.append(key)
        crawler._last_save_created = key not in known
        if crawler._last_save_created:
            known.add(key)
            created.append(key)

    crawler._save_paper = save
    crawler._report_source_failure = failures.append
    with patch.object(MODULE, '_curl_get', side_effect=fetch), patch.object(MODULE.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
        count = crawler.crawl(limit=limit)
    return count, attempted, created, requests, failures


class ArrowNewSaveLimitTests(unittest.TestCase):
    def test_existing_first_page_does_not_hide_later_new_records(self):
        count, attempted, created, requests, failures = replay(SOURCES[0]['external_ids'], limit=3)
        self.assertEqual(count, 3)
        self.assertEqual(created, SOURCES[1]['external_ids'] + SOURCES[2]['external_ids'])
        self.assertEqual(len(requests), 3)
        self.assertEqual(failures, [])

    def test_repeat_with_all_known_records_reports_zero_new_and_reads_all_pages(self):
        known = [key for source in SOURCES for key in source['external_ids']]
        count, attempted, created, requests, failures = replay(known)
        self.assertEqual(count, 0)
        self.assertEqual(created, [])
        self.assertEqual(attempted, known)
        self.assertEqual(len(requests), 3)

    def test_existing_records_within_a_page_do_not_consume_limit(self):
        count, attempted, created, requests, failures = replay(SOURCES[0]['external_ids'][:2], limit=1)
        self.assertEqual(count, 1)
        self.assertEqual(created, SOURCES[0]['external_ids'][2:])
        self.assertEqual(attempted, SOURCES[0]['external_ids'])
        self.assertEqual(len(requests), 1)

    def test_repeated_source_token_is_reported_as_failure(self):
        count, attempted, created, requests, failures = replay(repeat_first=True)
        self.assertEqual(count, 3)
        self.assertEqual(len(requests), 2)
        self.assertEqual(failures, ['invalid_response'])


if __name__ == '__main__':
    unittest.main()
