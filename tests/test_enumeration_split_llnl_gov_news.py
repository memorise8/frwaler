"""llnl-gov-news: contenthub JSONP listing (ext:jsonp) with offset path paging (ext:path_paging).

Memory-only: compares the plan's output with the unchanged crawl()'s save argument and runs the
shipped plan through the real SiteRunner with a fake transport.
"""
import contextlib
import copy
import io
import json
import re
import unittest
from unittest.mock import patch
from urllib.parse import urlencode, urlsplit

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, load, split_fixture, split_plan

SITE = 'llnl-gov-news'
LIST = re.compile(r'^/articles/all/20/(\d+)/none/www\.llnl\.gov$')
EMPTY = '/**/eventsLoaded([]);'


class LlnlSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # JSONP override of the shared check: the end page is an empty callback, not bare '[]'.
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        url = 'https://contenthub.llnl.gov/articles/all/20/40/none/www.llnl.gov?' + urlencode(p.listing['params'])
        page = parser.parse(Response(200, {'Content-Type': self.fixture['content_type']}, EMPTY, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        for body in ('<html>maintenance</html>', '[]', '/**/eventsLoaded([]); alert(1)'):
            with self.subTest(body=body):
                try:
                    bad = parser.parse(Response(200, {'Content-Type': 'text/html'}, body, url))
                except PermanentFailure as exc:
                    self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
                else:
                    self.assertTrue(bad.extra.get('parse_error'))
                    self.assertFalse(bad.extra.get('explicit_empty', False))

    def test_identity_is_nid_and_external_link_kept(self):
        papers = self.pipeline()
        self.assertEqual(['55121', '55100'], [p['post_number'] for p in papers])
        self.assertEqual(['https://www.llnl.gov/article/55121/one', 'https://example.org/story-two'],
                         [p['url'] for p in papers])


class LlnlSplitRun(unittest.TestCase):
    """Shipped plan end to end: offset 0 serves the fixture, later offsets the empty callback."""

    def setUp(self):
        self.denials = []
        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('split run attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def run_plan(self, first_body):
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE))
        plan['request_interval_s'] = 0
        seen = []

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            m = LIST.match(parts.path)
            if not m or parts.netloc != 'contenthub.llnl.gov' or parts.query != 'callback=eventsLoaded':
                return Response(404, {}, b'', url)
            seen.append(int(m.group(1)))
            body = first_body if m.group(1) == '0' else EMPTY
            return Response(200, {'Content-Type': fixture['content_type']}, body, url)

        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            return result, env.docs(), seen
        finally:
            env.close()

    def test_shipped_plan_completes_end_to_end(self):
        result, docs, seen = self.run_plan(split_fixture(SITE)['listing_body'])
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(['55121', '55100'], sorted({d['post_number'] for d in docs}, reverse=True))
        self.assertIn(0, seen)
        self.assertTrue(all(o % 20 == 0 for o in seen), seen)

    def test_error_listing_ends_incomplete(self):
        result, docs, _ = self.run_plan('<html><body>Site under maintenance</body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
