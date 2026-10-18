"""L3b-20261018 (ext:headers + ext:post_listing): enumeration split for
education-govt-nz-search.

The listing is the site's own Algolia index (date_desc_live), read with crawl()'s query
(facetFilters result.lvl1 'Documents > Corporate publication', filters status:true,
hitsPerPage 20, page 0..) from the single-index endpoint /1/indexes/date_desc_live/query
(POST JSON), whose answer is crawl()'s results[0]. The x-algolia-api-key header comes only
from the environment variable EDUCATION_GOVT_NZ_SEARCH_KEY and goes only to the Algolia host.
List-only: the paper is crawl()'s conversion of the hit (external_id = objectID,
post_number = numeric entity id, url = pdf_url = file_url). Memory only, synthetic data."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import PlanError, header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import load

SITE = 'education-govt-nz-search'
ROOT = Path(__file__).resolve().parents[1]
ENV = 'EDUCATION_GOVT_NZ_SEARCH_KEY'
HOST = 'zjyla4j4r6-dsn.algolia.net'
JSON = {'Content-Type': 'application/json'}


def fixture():
    return json.loads((ROOT / 'tests/fixtures/collector_splits' / (SITE + '.json')).read_text())


def plan_doc():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class Recording:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def request(self, method, url, *, headers=None, data=None):
        self.calls.append((method, url, dict(headers or {}), data))
        return self.handler(method, url, data)


class Split(unittest.TestCase):
    def setUp(self):
        self.fx = fixture()
        self.raw = plan_doc()['sites'][SITE]
        self.plan = site_plan(plan_doc(), SITE, registered=[SITE])
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def native(self):
        """Unchanged crawl() over the fixture; _request is replaced in memory."""
        crawler, _ = load(SITE)
        captured, bodies = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._source_failure = None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def request(url, method='GET', json=None, headers=None, **kw):
            bodies.append(json)
            page = json['requests'][0]['page']
            data = {'results': [self.fx['listing'] if page == 0 else self.fx['end']]}
            return SimpleNamespace(json=lambda: copy.deepcopy(data))
        crawler._request = request
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured, bodies

    def adapter(self):
        crawler, _ = load(SITE)
        return Adapter(SITE, self.plan.section('adapter'), None, crawler=crawler)

    def parser(self):
        return ListingParser(self.plan.listing['parser'], total_cfg=self.plan.listing['total'],
                             adapter=self.adapter())

    def test_paper_and_identity_equal_original_crawl(self):
        expected, bodies = self.native()
        # 5003 shares 5001's file_url (crawl()'s dedup), 5004 has no title, 'x' has a short abstract.
        self.assertEqual(['5001', '5002'], [p['post_number'] for p in expected])
        parser = self.parser()
        adapter = parser.adapter
        resp = Response(200, JSON, json.dumps(self.fx['listing']), self.raw['listing']['url'])
        items = parser.parse(resp).items
        # Every hit stays listed (nbHits accounting); skipped ones produce no paper.
        self.assertEqual(['entity:paragraph/5001:en', 'entity:paragraph/5002:en', 'entity:paragraph/5003:en',
                          'entity:paragraph/5004:en', 'x'], [i.key for i in items])
        actual = [p for p in (adapter.materialize(i, None) for i in items) if p is not None]
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        # The plan body is crawl()'s query (index in the URL path, page as the paging field).
        sent = dict(bodies[0]['requests'][0])
        self.assertEqual('date_desc_live', sent.pop('indexName'))
        self.assertEqual(0, sent.pop('page'))
        self.assertEqual(sent, self.raw['listing']['params'])
        self.assertTrue(self.raw['listing']['url'].endswith('/1/indexes/date_desc_live/query'))

    def test_api_key_only_from_the_environment(self):
        self.assertEqual({'x-algolia-application-id': 'ZJYLA4J4R6'}, self.raw['request_headers'])
        self.assertEqual({'x-algolia-api-key': ENV}, self.raw['request_headers_env'])
        with self.assertRaises(PlanError):
            resolve_headers(self.raw, {})
        self.assertEqual([HOST], header_hosts(self.raw))
        self.assertNotRegex(json.dumps(plan_doc()), r'[0-9a-f]{32}')

    def test_empty_page_is_explicit_and_error_objects_are_not(self):
        parser = self.parser()
        end = parser.parse(Response(200, JSON, json.dumps(self.fx['end']), 'https://x/'))
        self.assertEqual([], end.items)
        self.assertTrue(end.extra.get('explicit_empty'))
        self.assertEqual(5, end.total)
        for bad in ({'message': 'Invalid Application-ID or API key', 'status': 403}, {'hits': None},
                    {'results': [self.fx['listing']]}, []):
            with self.assertRaises(PermanentFailure):
                parser.parse(Response(200, JSON, json.dumps(bad), 'https://x/'))
        broken = parser.parse(Response(200, JSON, '<html>not json</html>', 'https://x/'))
        self.assertEqual('invalid_json', broken.extra.get('parse_error'))
        self.assertFalse(broken.extra.get('explicit_empty'))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_plan_posts_with_the_key_header_and_completes(self):
        from tests.enumeration_fakes import Env
        site = copy.deepcopy(self.raw)
        site['request_interval_s'] = 0

        def handle(method, url, data):
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'', url)
            self.assertEqual('POST', method)  # list-only: no detail or file request in the body run
            body = json.loads(data)
            out = self.fx['listing'] if body.get('page', 0) == 0 else self.fx['end']  # no page = first page
            return Response(200, JSON, json.dumps(out), url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            transport = Recording(handle)
            plan = env.plan(site)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn, 0), transport=transport,
                                 extra_headers=resolve_headers(plan.raw, {ENV: 'synthetic-test-key'}),
                                 header_hosts=header_hosts(plan.raw))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual((2, 3), (result['stats']['saved_new'], result['stats']['failed_permanent']))
        self.assertEqual({'entity:paragraph/5001:en', 'entity:paragraph/5002:en'}, {d['post_number'] for d in docs})  # stored = external_id
        posts = [c for c in transport.calls if c[0] == 'POST']
        self.assertTrue(posts and all(c[2].get('x-algolia-api-key') == 'synthetic-test-key' and
                                      c[2].get('x-algolia-application-id') == 'ZJYLA4J4R6' for c in posts))
        self.assertTrue(all(c[1].startswith('https://' + HOST + '/') for c in posts))
        self.assertEqual([0, 1], sorted({json.loads(c[3]).get('page', 0) for c in posts})[:2])


if __name__ == '__main__':
    unittest.main()
