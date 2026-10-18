"""NR-20261018: data-busan-go-kr-bdip enumeration split (crawl() unchanged), memory-only.
The list API is a POST JSON search (ext:post_listing); the fake transport reads the offset
from the request body."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan
from tests.nr_a_split_helpers import NRSplitChecks, NRSplitRunChecks

SITE = 'data-busan-go-kr-bdip'


class RegisteredSplit(NRSplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, bodies = [], []

        def post(url, body):
            bodies.append(body)
            return json.loads(fixture['listing_body'] if len(bodies) == 1 else fixture['empty_body'])

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl_post_json = post
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual([0, 50], [b['offset'] for b in bodies])
        return captured, []


class PostTransport:
    def __init__(self, listing, ctype, empty):
        self.listing, self.ctype, self.empty, self.bodies = listing, ctype, empty, []

    def request(self, method, url, *, headers=None, data=None):
        if url.endswith('/robots.txt'):
            return Response(404, {}, b'', url)
        assert method == 'POST', method
        body = json.loads(data)
        self.bodies.append(body)
        return Response(200, {'Content-Type': self.ctype}, self.listing if body['offset'] == 0 else self.empty, url)


class RegisteredSplitRun(NRSplitRunChecks, unittest.TestCase):
    SITE = SITE

    def _run(self, listing, ctype):
        from tests.enumeration_fakes import Env
        plan = copy.deepcopy(split_plan(SITE)); plan['request_interval_s'] = 0
        transport = PostTransport(listing, ctype, self.fixture['empty_body'])
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, None, crawler=cls(env.conn), transport=transport)
            return result, env.docs(), transport
        finally:
            env.close()

    def test_shipped_plan_completes_end_to_end(self):
        result, docs, transport = self._run(self.fixture['listing_body'], self.fixture['content_type'])
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual({'15092750', '3057229'}, {d['post_number'] for d in docs})
        self.assertTrue(all(b['pagelength'] == 50 and b['searchSort'] == 'REG_DT' for b in transport.bodies))

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        cases = {'maintenance_page': ('<html><body><p>Site under maintenance</p></body></html>', 'text/html'),
                 'json_error_object': (json.dumps({'error': 'x'}), 'application/json')}
        for name, (body, ctype) in cases.items():
            with self.subTest(case=name):
                result, docs, _ = self._run(body, ctype)
                self.assertEqual('incomplete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
                self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        f = split_fixture(SITE)
        listing = f['listing_body'].replace(json.dumps(f['long_text'], ensure_ascii=False)[1:-1], 'Short.')
        result, docs, _ = self._run(listing, f['content_type'])
        self.assertEqual([], docs)
        self.assertNotEqual('complete', result['status'], result)


if __name__ == '__main__':
    unittest.main()
