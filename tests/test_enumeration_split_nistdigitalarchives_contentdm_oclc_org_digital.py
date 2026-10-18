"""L3b-20261018: enumeration split for nistdigitalarchives-contentdm-oclc-org-digital.
CONTENTdm dmQuery puts the start offset inside the q path, so the plan reads fixed
offset windows of 1024 records as explicit segments ({segment} = start offset,
sorted by dmrecord); the plan's &page=N is not part of the path, so page 2 of a window
is declared empty. Detail = dmGetItemInfo JSON, used only for short list abstracts
(as in crawl()). Memory-only: no network or subprocess (the end-to-end run uses a
temporary SQLite)."""
import contextlib
import copy
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit

from crawler.enumeration.http import Response
from tests.collector_split_helpers import SplitChecks, load

SITE = 'nistdigitalarchives-contentdm-oclc-org-digital'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def setUp(self):
        super().setUp()
        # The pipeline helper reads the first page of the listing URL; give it window 1.
        listing = self.plan['sites'][SITE]['listing']
        listing['url'] = listing['url'].replace('{segment}', '1')

    def native(self, fixture=None):
        # crawl() asks dmQuery for the total first (page size 1), then reads one window;
        # every list call gets the fixture listing. crawl() walks a page newest-first
        # (reversed); the result is returned in listing order for comparison.
        fixture = fixture or self.fixture
        crawler, _ = load(self.SITE)
        captured, calls, list_calls = [], [], []

        def transport(url, *args, **kwargs):
            calls.append(url)
            if url in fixture['details']:
                return json.dumps(fixture['details'][url], ensure_ascii=False)
            list_calls.append(url)
            return fixture['listing_body']
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._curl_get = transport
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual(2, len(list_calls))
        return list(reversed(captured)), calls

    def test_valid_empty_page_and_error_page_are_distinct(self):
        from crawler.enumeration.adapters import Adapter
        from crawler.enumeration.http import PermanentFailure
        from crawler.enumeration.parsing import ListingParser
        from crawler.enumeration.plan import site_plan
        p = site_plan(self.plan, self.SITE, registered=[self.SITE])
        crawler, _ = load(self.SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(self.SITE, p.section('adapter'), None, crawler=crawler))
        url = p.listing['url'] + '&page=2'
        page = parser.parse(Response(200, {'Content-Type': 'application/json'}, self.fixture['listing_body'], url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        error_page = parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', url))
        self.assertTrue(error_page.extra.get('parse_error'))
        with self.assertRaises(PermanentFailure):
            parser.parse(Response(200, {'Content-Type': 'application/json'}, '[]', url))

    def test_collection_larger_than_windows_is_a_parse_error(self):
        from crawler.enumeration.guard import guarded
        from crawler.enumeration.http import PermanentFailure
        crawler, _ = load(self.SITE)
        data = json.loads(self.fixture['listing_body'])
        data['pager']['total'] = crawler._ENUMERATION_CAPACITY + 1
        with self.assertRaises(PermanentFailure):
            with guarded():
                crawler._enumeration_items(data, 'https://x.test/?q=dmQuery')

    def test_segments_cover_the_capacity_in_windows(self):
        crawler, _ = load(self.SITE)
        site = json.loads((self.plan_path()).read_text())['sites'][SITE]
        keys = [i['key'] for i in site['segments']['items']]
        w = crawler._ENUMERATION_WINDOW
        self.assertEqual([str(1 + w * i) for i in range(crawler._ENUMERATION_CAPACITY // w)], keys)
        q = unquote(parse_qs(urlsplit(site['listing']['url']).query)['q'][0])
        self.assertIn(f'/dmrecord/{w}/{{segment}}/', q)
        self.assertEqual(w, site['listing']['page_size'])

    def plan_path(self):
        from tests.collector_split_helpers import ROOT
        return ROOT / 'docs/examples/collector-splits' / (SITE + '.json')

    def test_post_number_is_pointer_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['900001', '900002'], [p['external_id'] for p in expected])


class RegisteredSplitRun(unittest.TestCase):
    def test_shipped_plan_completes_end_to_end(self):
        """Shipped plan through the real SiteRunner.run; the segment list is cut to the
        fixture's one window (the real collection fills all nine)."""
        from tests.collector_split_helpers import split_fixture, split_plan
        from tests.enumeration_fakes import Env
        fx = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE))
        plan['request_interval_s'] = 0
        plan['segments']['items'] = plan['segments']['items'][:1]

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fx['details']:
                return Response(200, {'Content-Type': 'application/json'}, json.dumps(fx['details'][url]), url)
            q = unquote(parse_qs(parts.query).get('q', [''])[0])
            if q.startswith('dmGetItemInfo/'):
                key = fx['details'].get(url.replace(parts.query, 'q=' + q))
                return Response(200, {'Content-Type': 'application/json'}, json.dumps(key), url)
            self.assertIn('/dmrecord/1024/1/', q)
            return Response(200, {'Content-Type': 'application/json'}, fx['listing_body'], url)

        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(['900001', '900002'], sorted(d['post_number'] for d in docs))


if __name__ == '__main__':
    unittest.main()
