"""policyalternatives-ca-news-research split: WP REST Reports posts (category 2812), one row -> one
paper with crawl's fields; authors from embedded 'author' terms instead of the /coauthors call."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'policyalternatives-ca-news-research'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        crawler._author_cache = {}
        captured, calls = [], []

        def curl_json(url, params=None, retries=3):
            calls.append((url, dict(params or {})))
            if url.endswith('/coauthors'):
                wanted = {int(x) for x in str(params['include']).split(',')}
                return [a for a in fixture['coauthors'] if a['id'] in wanted]
            return json.loads(fixture['listing_body']) if params.get('page') == 1 else []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(module, '_curl_json', curl_json))
            stack.enter_context(patch('time.sleep', lambda *a: None))
            with contextlib.redirect_stdout(io.StringIO()):
                result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, calls

    def test_authors_match_coauthors_endpoint(self):
        expected, _ = self.native()
        self.assertEqual('David Macdonald; Noelle Greuel', expected[0]['authors'])
        self.assertEqual([clean['authors'] for clean in expected], [p['authors'] for p in self.pipeline()])

    def test_listing_error_object_is_a_parse_error(self):
        crawler, _ = load(self.SITE)
        from crawler.enumeration.http import PermanentFailure
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'code': 'rest_no_route'})


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE
