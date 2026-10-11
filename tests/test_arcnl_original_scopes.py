import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('arcnl_original_scopes', ROOT / 'crawler/sites/custom/arcnl-nl-publications.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
CLS = next(c for c in vars(MODULE).values() if isinstance(c, type) and getattr(c, 'site_id', None) == 'arcnl-nl-publications')
FIXTURES = ROOT / 'tests/fixtures/arcnl_scopes'
SOURCES = json.loads((FIXTURES / 'sources.json').read_text())


def replay(limit=None, existing=None):
    crawler = object.__new__(CLS)
    crawler._delay = 0
    requests, papers = [], []

    def fetch(url):
        requests.append(url)
        for i, source in enumerate(SOURCES):
            if url == source['list_url']:
                return (FIXTURES / f'list-{i}.html').read_text()
            if url == source['list_url'] + '/page/2':
                return '<main>No publications</main>'
            if url == source['detail_url']:
                return (FIXTURES / f'detail-{i}.html').read_text()
        raise AssertionError(url)

    def save(paper):
        papers.append(paper)
        crawler._last_save_created = paper['post_number'] not in (existing or set())

    crawler._save_paper = save
    with patch.object(MODULE, '_curl_get', side_effect=fetch), patch.object(MODULE.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()):
        count = crawler.crawl(limit=limit)
    return count, papers, requests


class ARCNLOriginalScopesTests(unittest.TestCase):
    def test_all_three_original_thesis_types_keep_source_ids_and_dates(self):
        count, papers, requests = replay()
        self.assertEqual(count, 3)
        self.assertEqual([p['category'] for p in papers], ['bachelorthesis', 'masterthesis', 'phdthesis'])
        self.assertEqual([p['post_number'] for p in papers], ['6625', '6837', '7211'])
        self.assertEqual([p['published_date'] for p in papers], ['2025-08-23', '2025-07-18', '2026-09-29'])
        self.assertEqual([p['url'] for p in papers], [s['detail_url'] for s in SOURCES])
        self.assertTrue(all(p['pdf_url'].startswith('https://request.pub.arcnl.nl/request?att_id=') for p in papers))
        self.assertTrue(all(s['list_url'] + '/page/2' in requests for s in SOURCES))

    def test_limit_is_shared_across_scopes(self):
        count, papers, requests = replay(limit=2)
        self.assertEqual(count, 2)
        self.assertEqual(len(papers), 2)
        self.assertNotIn(SOURCES[2]['list_url'], requests)

    def test_existing_bachelor_does_not_consume_new_save_limit(self):
        count, papers, requests = replay(limit=1, existing={'6625'})
        self.assertEqual(count, 1)
        self.assertEqual([p['post_number'] for p in papers], ['6625', '6837'])
        self.assertNotIn(SOURCES[2]['list_url'], requests)


if __name__ == '__main__':
    unittest.main()
