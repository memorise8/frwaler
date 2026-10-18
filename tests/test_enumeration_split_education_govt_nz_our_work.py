"""L3b-20261018 (ext:headers + ext:post_listing): enumeration split for
education-govt-nz-our-work.

The listing is the site's own Algolia index (date_desc_live), read with crawl()'s filter and
page size (hitsPerPage 20, page 0..) from the single-index endpoint
/1/indexes/date_desc_live/query (POST JSON), whose answer is crawl()'s results[0]. The
x-algolia-api-key header comes only from the environment variable
EDUCATION_GOVT_NZ_OUR_WORK_KEY (never a literal in the plan) and goes only to the Algolia
host. The paper is crawl()'s conversion (external_id = post_number = Drupal node id) with
the detail page's RSC body text. Memory only, synthetic data, no real key."""
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

SITE = 'education-govt-nz-our-work'
ROOT = Path(__file__).resolve().parents[1]
ENV = 'EDUCATION_GOVT_NZ_OUR_WORK_KEY'
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
        """Unchanged crawl() over the fixture; curl (subprocess.run) is replaced in memory."""
        crawler, module = load(SITE)
        captured, posts = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def run(cmd, capture_output=True, text=False, timeout=None):
            if '-X' in cmd:
                posts.append(json.loads(cmd[cmd.index('-d') + 1]))
                page = posts[-1]['requests'][0]['page']
                out = json.dumps({'results': [self.fx['listing'] if page == 0 else self.fx['end']]})
                return SimpleNamespace(stdout=out)
            body = self.fx['details'].get(cmd[-1], '')
            return SimpleNamespace(stdout=body.encode('utf-8'))
        with patch.object(module.subprocess, 'run', run), patch('time.sleep', lambda *a: None), \
                contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured, posts

    def adapter(self):
        crawler, _ = load(SITE)
        return Adapter(SITE, self.plan.section('adapter'), None, crawler=crawler)

    def test_paper_and_identity_equal_original_crawl(self):
        expected, posts = self.native()
        self.assertEqual(['9001', '9002'], [p['post_number'] for p in expected])  # 9003: short abstract
        adapter = self.adapter()
        resp = Response(200, JSON, json.dumps(self.fx['listing']), self.raw['listing']['url'])
        items = ListingParser(self.plan.listing['parser'], adapter=adapter).parse(resp).items
        # 9004 (no title) stays listed for the nbHits count; its paper is skipped like crawl().
        self.assertEqual(['9001', '9002', '9003', '9004'], [i.key for i in items])

        def get(url):
            return Response(200, {'Content-Type': 'text/html'}, self.fx['details'][url], url)
        actual = [p for p in (adapter.materialize(i, SimpleNamespace(get=get)) for i in items) if p is not None]
        self.assertEqual([dict(clean_paper(p), site_id=SITE) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        self.assertGreater(len(actual[0]['abstract']), len(self.fx['listing']['hits'][0]['content']))  # RSC body won
        # The plan body is crawl()'s query (index in the URL path, page as the paging field).
        sent = dict(posts[0]['requests'][0])
        self.assertEqual('date_desc_live', sent.pop('indexName'))
        self.assertEqual(0, sent.pop('page'))
        self.assertEqual(sent, self.raw['listing']['params'])
        self.assertTrue(self.raw['listing']['url'].endswith('/1/indexes/date_desc_live/query'))
        self.assertEqual(crawler_filter(), self.raw['listing']['params']['filters'])

    def test_api_key_only_from_the_environment(self):
        self.assertEqual({'x-algolia-application-id': 'ZJYLA4J4R6'}, self.raw['request_headers'])
        self.assertEqual({'x-algolia-api-key': ENV}, self.raw['request_headers_env'])
        with self.assertRaises(PlanError):
            resolve_headers(self.raw, {})
        self.assertEqual([HOST], header_hosts(self.raw))
        text = json.dumps(plan_doc())
        self.assertNotRegex(text, r'[0-9a-f]{32}')

    def test_empty_page_is_explicit_and_error_objects_are_not(self):
        parser = ListingParser(self.plan.listing['parser'], total_cfg=self.plan.listing['total'],
                               adapter=self.adapter())
        end = parser.parse(Response(200, JSON, json.dumps(self.fx['end']), 'https://x/'))
        self.assertEqual([], end.items)
        self.assertTrue(end.extra.get('explicit_empty'))
        self.assertEqual(4, end.total)
        # A hit without url has neither identity nor detail page: left out (crawl() skips it).
        crawler, _ = load(SITE)
        crawler.base_url = 'https://www.education.govt.nz'
        self.assertEqual([], crawler._enumeration_items({'hits': [{'objectID': 'entity:node/1:en', 'title': 'T'}]}))
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
            if method == 'GET':
                return Response(200, {'Content-Type': 'text/html'}, self.fx['details'][url], url)
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
        self.assertEqual((2, 2), (result['stats']['saved_new'], result['stats']['failed_permanent']))
        self.assertEqual({'9001', '9002'}, {d['post_number'] for d in docs})
        posts = [c for c in transport.calls if c[0] == 'POST']
        self.assertTrue(posts and all(c[2].get('x-algolia-api-key') == 'synthetic-test-key' and
                                      c[2].get('x-algolia-application-id') == 'ZJYLA4J4R6' for c in posts))
        self.assertTrue(all(c[1].startswith('https://' + HOST + '/') for c in posts))
        details = [c for c in transport.calls if c[0] == 'GET' and not c[1].endswith('/robots.txt')]
        self.assertTrue(details and all('x-algolia-api-key' not in c[2] for c in details))  # key stays on Algolia
        self.assertEqual([0, 1], sorted({json.loads(c[3]).get('page', 0) for c in posts})[:2])


def crawler_filter():
    _, module = load(SITE)
    cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
    return cls._ALGOLIA_FILTER


if __name__ == '__main__':
    unittest.main()
