"""L4-20261018: enumeration split for prism-go-kr-homepage (ext:post_listing, POST JSON totalSearch.do,
startCount offset 20/page, paper from the list row only). Memory-only: crawl() with a fake _curl_post
versus the plan's list/paper methods through the real Adapter and ListingParser."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load

SITE = 'prism-go-kr-homepage'


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class PrismSplit(unittest.TestCase):
    def setUp(self):
        def denied(*a, **k):
            raise AssertionError('network/process I/O attempted')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)

    def native(self, body):
        crawler, _ = load(SITE)
        saved, posts = [], []
        def fake_post(payload):
            posts.append(payload)
            return body if len(posts) == 1 else json.dumps({'report': {'searchResultList': []}})
        crawler._curl_post = fake_post
        crawler._delay = 0
        crawler._last_save_created = True
        crawler.delivery_cursor = None
        crawler._advance_cursor = lambda *a, **k: None
        crawler._mark_exhausted = lambda: None
        crawler._save_paper = lambda p: saved.append(copy.deepcopy(p))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=None)
        self.assertEqual(0, posts[0]['startCount'])
        return saved

    def pipeline(self, body):
        p = site_plan(plan(), SITE, registered=[SITE])
        crawler, _ = load(SITE)
        adapter = Adapter(SITE, p.section('adapter'), None, crawler=crawler)
        page = ListingParser(p.listing['parser'], adapter=adapter).parse(
            Response(200, {'Content-Type': 'application/json'}, body, p.listing['url']))
        out = []
        for item in page.items:
            paper = adapter.materialize(item, None)
            if paper is not None:
                out.append(paper)
        return page, out

    def test_papers_and_identity_equal_original_crawl(self):
        body = fixture()['listing_body']
        expected = self.native(body)
        self.assertEqual(3, len(expected))
        page, actual = self.pipeline(body)
        self.assertEqual(4, len(page.items))  # the id-less row is skipped like crawl()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(clean_paper(p)) for p in expected], [paper_identity(p) for p in actual])
        self.assertEqual(['1111-2026_000_D1_1', '2222-2026', '3333_000'], [p['external_id'] for p in expected])

    def test_empty_page_is_explicit_end_and_error_object_is_parse_error(self):
        page, _ = self.pipeline(json.dumps({'report': {'searchResultList': []}}))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        crawler, _ = load(SITE)
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items({'error': 'maintenance'})
        with self.assertRaises(PermanentFailure):
            crawler._enumeration_items(['not', 'an', 'object'])

    def test_plan_is_post_offset_listing(self):
        p = site_plan(plan(), SITE, registered=[SITE])
        self.assertEqual('POST', p.listing['method'])
        self.assertTrue(p.listing['idempotent'])
        self.assertEqual(('startCount', 'offset', 20), (p.listing['page_param'], p.listing['page_mode'], p.listing['page_size']))


if __name__ == '__main__':
    unittest.main()
