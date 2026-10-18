"""NR2-20261018: naturalengland-defra-opendata-arcgis-com-search enumeration split (crawl()
unchanged), memory-only. Papers come from the OGC items page alone (paper_method)."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import load
from tests.nr2_a_split_helpers import NR2SplitChecks, NRSplitRunChecks

SITE = 'naturalengland-defra-opendata-arcgis-com-search'


class RegisteredSplit(NR2SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(SITE)
        captured, calls = [], []
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def curl_json(url, *a, **k):
            calls.append(url)
            return json.loads(fixture['listing_body'] if len(calls) == 1 else fixture['empty_body'])
        with patch.object(module, '_curl_json', curl_json), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        return captured, calls

    def test_short_abstract_skip_is_preserved(self):
        f = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        expected, _ = self.native(f)
        self.assertEqual([], expected)
        self.assertEqual([], self.pipeline(f))


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = SITE
    END_STATUS = 200

    def test_shipped_plan_completes_end_to_end(self):
        from crawler.enumeration.http import Response
        from tests.collector_split_helpers import run_split
        body = self.fixture['empty_body']

        def end(url):
            return Response(200, {'Content-Type': 'application/json'}, body, url)
        # run_split keys pages on ?page=; this plan pages on startindex, so serve by offset.
        from urllib.parse import parse_qs, urlsplit
        listing = self.fixture['listing_body']

        def handler_end(url):
            q = parse_qs(urlsplit(url).query)
            return end(url) if q.get('startindex', ['1'])[0] != '1' else Response(
                200, {'Content-Type': 'application/json'}, listing, url)
        result, docs = run_split(SITE, end=handler_end, plan=None)
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
