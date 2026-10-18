"""mi-se-arkiv: the three WP REST post types crawl() reads (publication, referral-response,
posts) as explicit segments of ?rest_route=/wp/v2/<type>; paper compared with the unchanged crawl()."""
import contextlib
import copy
import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import tests.collector_split_helpers as base
from crawler.enumeration.http import Response
from tests.l2b_g4_split_helpers import G4SplitChecks

SITE = 'mi-se-arkiv'


class RegisteredSplit(G4SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        """crawl() through module _curl: publication page 1 = fixture, everything else empty."""
        fixture = fixture or self.fixture
        crawler, module = base.load(SITE)
        captured, list_calls = [], []

        def curl(url, retries=3):
            if url in fixture['details']:
                return fixture['details'][url]
            list_calls.append(url)
            if '/wp/v2/publication?' in url and 'page=1&' in url:
                return fixture['listing_body']
            return '[]'

        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch.object(module, '_curl', curl), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured, list_calls


class RegisteredSplitRun(unittest.TestCase):
    def setUp(self):
        self.fixture = base.split_fixture(SITE)
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def run_plan(self, broken=None):
        from tests.enumeration_fakes import Env
        f = self.fixture
        lists = dict(f['extra_segments'], publication=f['listing_body'])
        details = dict(f['details'], **f['extra_details'])
        plan = copy.deepcopy(base.split_plan(SITE))
        plan['request_interval_s'] = 0

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in details:
                return Response(200, {'Content-Type': 'text/html'}, details[url], url)
            q = parse_qs(parts.query)
            kind = q['rest_route'][0].rsplit('/', 1)[-1]
            body = lists[kind]
            if int(q.get('page', ['1'])[0]) > 1:
                return base.wp_past_last_page(url)
            if kind == broken:
                return Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url)
            return Response(200, {'Content-Type': 'application/json', 'X-WP-Total': str(len(json.loads(body)))},
                            body, url)

        env = Env(SITE)
        try:
            _, module = base.load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn, 0))
            return result, env.docs()
        finally:
            env.close()

    def test_all_three_post_types_complete_end_to_end(self):
        result, docs = self.run_plan()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(4, len(docs))
        self.assertEqual({'rapport-ett', 'rapport-tva', 'remissvar-ett', 'nyhet-ett'}, {d['post_number'] for d in docs})
        pdf = {d['post_number']: d['pdf_url'] for d in docs}
        self.assertTrue(pdf['rapport-ett'].endswith('/rapport-ett.pdf'))
        self.assertFalse(pdf['nyhet-ett'])  # crawl() takes PDFs only for publications

    def test_broken_segment_listing_is_not_complete(self):
        result, _ = self.run_plan(broken='posts')
        self.assertNotEqual('complete', result['status'], result)


if __name__ == '__main__':
    unittest.main()
