"""ND-2: nidcd-nih-gov-news own-host listing scope (user-delegated 10-19), memory-only.
_enumeration_items_own_host = crawl()'s rows minus rows whose article is on another host."""
import copy
import unittest
from unittest.mock import patch

from crawler.enumeration.guard import guarded
from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, run_split, split_fixture, split_plan

SITE = 'nidcd-nih-gov-news'
URL = 'https://www.nidcd.nih.gov/news/nidcd-news?page=0'


class NidcdOwnHost(unittest.TestCase):
    def setUp(self):
        self.fixture = split_fixture(SITE)
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O')); p.start(); self.addCleanup(p.stop)

    def test_same_rows_when_all_on_own_host(self):
        c, _ = load(SITE)
        with guarded():
            self.assertEqual(c._enumeration_items(self.fixture['listing_body'], URL),
                             c._enumeration_items_own_host(self.fixture['listing_body'], URL))

    def test_other_host_rows_left_out(self):
        c, _ = load(SITE)
        body = self.fixture['listing_body'].replace('/news/2025/hearing', 'https://www.nih.gov/news-events/hearing', 1)
        with guarded():
            all_rows = c._enumeration_items(body, URL)
            own = c._enumeration_items_own_host(body, URL)
        self.assertEqual(2, len(all_rows))
        self.assertEqual(1, len(own))
        self.assertEqual(all_rows[0], own[0])

    def test_plan_with_own_host_listing_completes(self):
        plan = copy.deepcopy(split_plan(SITE))
        plan['adapter']['list_method'] = '_enumeration_items_own_host'
        fx = self.fixture
        result, docs = run_split(SITE, plan=plan, end=lambda url: Response(200, {'Content-Type': fx['content_type']},
                                                                            fx['empty_body'], url))
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(2, len(docs))


if __name__ == '__main__':
    unittest.main()
