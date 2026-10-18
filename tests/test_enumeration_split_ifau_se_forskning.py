"""ifau-se-forskning: POST SearchQep listing (ext:post_listing) + pure detail conversion.

crawl() lists reports by POSTing {"Years": [], "SubjectCategories": [], "Query": "", "Page": N}
(JSON) to /Forskning/Publikationer/Rapporter/SearchQep/ and keeps the links matching
``_LINK_RE`` (a report under a "1998-1999" folder never matches, so crawl() skips it).
The plan sends the same read-only search POST (``listing.idempotent``); each report page
is converted by ``_enumeration_paper(html, request_url)``, which must equal crawl()'s
saved paper. A fragment past the last page ("Visar 611-610 ...", empty <ul>) is a valid
empty page; anything else is a listing parse error.
"""

import contextlib
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.guard import guarded
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load, split_plan

SITE = 'ifau-se-forskning'
SEARCH = 'https://www.ifau.se/Forskning/Publikationer/Rapporter/SearchQep/'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        fixture = fixture or self.fixture
        crawler, module = load(self.SITE)
        captured = []; calls = []; pages = []

        def post(page):
            pages.append(page)
            return SimpleNamespace(text=fixture['listing_body'] if page == 1 else fixture['empty_body'])

        def curl(url, *args, **kwargs):
            calls.append(url)
            return fixture['details'][url]

        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._post_list_page = post
        with patch.object(module, '_curl', curl), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        self.assertEqual([1, 2], pages)
        return captured, calls

    def test_module_helpers_are_unchanged(self):
        import ast, hashlib
        from tests.collector_split_helpers import ROOT
        code = (ROOT/'crawler/sites/custom'/(self.SITE+'.py')).read_text()
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in ast.parse(code).body if isinstance(n, ast.FunctionDef)}
        self.assertEqual(self.fixture['module_function_source_sha256'], actual)

    def test_post_body_equals_crawl_request(self):
        crawler, _ = load(self.SITE)
        sent = []
        crawler._request = lambda url, **kw: sent.append((url, kw))
        crawler._post_list_page(7)
        site = self.plan['sites'][SITE]
        listing = site['listing']
        body = dict(listing['params'], **{listing['page_param']: 7})
        self.assertEqual([(SEARCH, {'method': 'POST', 'json': body})], sent)
        self.assertEqual((listing['url'], 'POST', 'json', True),
                         (SEARCH, listing['method'], listing['body_type'], listing['idempotent']))

    def test_listing_keeps_crawl_link_rule(self):
        _, calls = self.native()
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['listing_body'], SEARCH))
        self.assertEqual(calls, [i.url for i in page.items])
        self.assertNotIn('1998-1999', ' '.join(i.url for i in page.items))

    def test_identity_is_report_number(self):
        papers = self.pipeline()
        self.assertEqual(self.fixture['expected_ids'], [p['external_id'] for p in papers])
        self.assertEqual(list(self.fixture['details']), [p['url'] for p in papers])

    def test_added_listing_rejects_wrong_shape(self):
        crawler, _ = load(self.SITE)
        for body in ({'error': 'not a listing'}, '<html><body>maintenance</body></html>',
                     '<div class="list"><a href="/Forskning/Publikationer/Rapporter/2026/x/">x</a></div>'):
            with self.assertRaises(PermanentFailure) as ctx:
                with guarded():
                    crawler._enumeration_search_items(body, SEARCH)
            self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        parser = ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))
        page = parser.parse(Response(200, {'Content-Type': 'text/html'}, self.fixture['empty_body'], SEARCH))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        self.assertNotIn('parse_error', page.extra)
        with self.assertRaises(PermanentFailure) as ctx:
            parser.parse(Response(200, {'Content-Type': 'text/html'}, '<html>maintenance</html>', SEARCH))
        self.assertTrue(ctx.exception.reason.startswith('listing_parse_error:'))


class PostTransport:
    """POST SearchQep by JSON body Page; GET report pages. Records method/url/body."""

    def __init__(self, fixture, listing=None):
        self.fixture = fixture
        self.listing = fixture['listing_body'] if listing is None else listing
        self.calls = []

    @property
    def urls(self):
        return [c[1] for c in self.calls]

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, data))
        if urlsplit(url).path == '/robots.txt':
            return Response(404, {}, b'')
        if url == SEARCH and method == 'POST':
            page = json.loads(data)['Page']
            return Response(200, {'Content-Type': 'text/html'}, self.listing if page <= 1 else self.fixture['empty_body'], url)
        if method == 'GET' and url in self.fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, self.fixture['details'][url], url)
        return Response(404, {'Content-Type': 'text/html'}, '<html>not found</html>', url)


def run_post(fixture, listing=None):
    from tests.enumeration_fakes import Env
    plan = copy.deepcopy(split_plan(SITE))
    plan['request_interval_s'] = 0
    transport = PostTransport(fixture, listing)
    env = Env(SITE)
    try:
        _, module = load(SITE)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, None, crawler=cls(env.conn), transport=transport)
        return result, env.docs(), transport
    finally:
        env.close()


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE

    def test_shipped_plan_completes_end_to_end(self):
        result, docs, transport = run_post(self.fixture)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(sorted(self.fixture['expected_ids']), sorted(d['post_number'] for d in docs))
        posts = [json.loads(d) for m, u, d in transport.calls if m == 'POST']
        self.assertTrue(posts)
        self.assertTrue(all(set(b) == {'Years', 'SubjectCategories', 'Query', 'Page'} for b in posts), posts)
        self.assertFalse([u for u in transport.urls if '1998-1999' in u])

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        result, docs, _ = run_post(self.fixture, listing='<html><body><p>Site under maintenance</p></body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)

    def test_short_paper_does_not_silently_become_complete(self):
        fixture = json.loads(json.dumps(self.fixture, ensure_ascii=False).replace(self.fixture['long_text'], 'Short.'))
        result, docs, _ = run_post(fixture)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
