import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]


class SiseministeeriumScope(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location(
            'siseministeerium_scope',
            ROOT / 'crawler/sites/custom/siseministeerium-ee-otsing.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.crawler = object.__new__(module.SiseministeeriumEeOtsingCrawler)
        self.fixture = (ROOT / 'tests/fixtures/siseministeerium/article-first-page.json').read_text()

    def test_original_article_scope_uses_published_widget_filter(self):
        def fetch(url):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query, {'filters[type]': ['Artikkel'], 'sort_by': ['created'],
                                     'page': ['1'], 'langcode': ['et'], 'limit': ['10']})
            return self.fixture
        with patch.object(self.crawler, '_curl', side_effect=fetch):
            docs, total = self.crawler._fetch_page(1)
        self.assertEqual(total, 166)
        self.assertEqual(len(docs), 10)
        self.assertEqual({x['content_type'] for x in docs}, {'article'})
        self.assertEqual(docs[0]['id'], '3oiwb2-content-entity:node/4326:et')
        self.assertEqual(docs, json.loads(self.fixture)['response']['docs'])

    def test_crawl_starts_at_first_source_page(self):
        with patch.object(self.crawler, '_fetch_page', return_value=([], 0)) as fetch:
            self.assertEqual(self.crawler.crawl(), 0)
        fetch.assert_called_once_with(1)

    def test_following_page_keeps_scope(self):
        with patch.object(self.crawler, '_curl', return_value=self.fixture) as fetch:
            self.crawler._fetch_page(2)
        query = parse_qs(urlsplit(fetch.call_args.args[0]).query)
        self.assertEqual(query['page'], ['2'])
        self.assertEqual(query['filters[type]'], ['Artikkel'])
        self.assertNotIn('type', query)


if __name__ == '__main__':
    unittest.main()
