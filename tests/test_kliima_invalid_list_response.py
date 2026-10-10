"""A retained HTTP 200 JSON null must not crash or become a zero-document result."""
import contextlib
import importlib.util
import io
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load(raw):
    spec = importlib.util.spec_from_file_location(
        'kliima_invalid_response', ROOT / 'crawler/sites/custom/kliimaministeerium-ee-otsing.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls = next(value for value in vars(module).values()
               if isinstance(value, type) and getattr(value, 'site_id', None) == 'kliimaministeerium-ee-otsing')
    obj = object.__new__(cls)
    obj._source_failure = None
    requested = []

    def fetch(url):
        requested.append(url)
        return raw

    obj._curl = fetch
    return obj, requested


class KliimaInvalidResponse(unittest.TestCase):
    def test_retained_null_payload_stops_native_crawl_with_unknown_total(self):
        raw = (Path(__file__).parent / 'fixtures/kliima_null/retained-response.json').read_text()
        obj, requested = load(raw)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(obj.crawl(), 0)
        self.assertEqual(len(requested), 1)
        self.assertEqual(obj.delivery_source_failure, 'invalid_response')
        self.assertIn('total unknown', output.getvalue())
        self.assertNotIn('end of results', output.getvalue())

    def test_valid_empty_response_remains_distinct_from_failure(self):
        obj, _ = load('{"response":{"numFound":0,"docs":[]}}')
        self.assertEqual(obj._fetch_page(0), ([], 0))
        self.assertIsNone(obj.delivery_source_failure)

    def test_invalid_shapes_do_not_fabricate_a_total(self):
        for payload in [[], {}, {'response': None}, {'response': {'numFound': 5}},
                        {'response': {'numFound': False, 'docs': []}},
                        {'response': {'numFound': 1, 'docs': [None]}}]:
            with self.subTest(payload=payload):
                obj, _ = load(json.dumps(payload))
                self.assertEqual(obj._fetch_page(0), ([], None))
                self.assertEqual(obj.delivery_source_failure, 'invalid_response')

    def test_valid_documents_and_total_are_preserved(self):
        docs = [{'id': 'entity:node/42:et', 'title': 'Source title', 'created': '2026-10'}]
        obj, _ = load(json.dumps({'response': {'numFound': 12, 'docs': docs}}))
        self.assertEqual(obj._fetch_page(0), (docs, 12))
        self.assertIsNone(obj.delivery_source_failure)


if __name__ == '__main__':
    unittest.main()
