"""L3b-20261018 (ext:headers + ext:post_listing): enumeration split for
transparency-gov-au-publications.

The listing is the site's own Azure Cognitive Search index, read with the same read-only
POST body as crawl() (filter annual_report|corp_plan|pbs, orderby ReportingYear desc,
top 50, skip = offset). The api-key header comes only from the environment variable
TRANSPARENCY_GOV_AU_PUBLICATIONS_KEY (never a literal in the plan). The paper is crawl()'s
_build_paper (external_id = post_number = index row ID). Memory only, no real key."""
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
from crawler.enumeration.plan import header_hosts, resolve_headers, site_plan
from tests.collector_split_helpers import load

SITE = 'transparency-gov-au-publications'
ROOT = Path(__file__).resolve().parents[1]
ENV = 'TRANSPARENCY_GOV_AU_PUBLICATIONS_KEY'
HOST = 'acs-dof-shared-aue-01.search.windows.net'


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
        crawler, _ = load(SITE)
        captured, bodies = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._source_failure = None
        crawler._check_cancelled = lambda: None
        crawler._report_source_failure = lambda *a, **k: None
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def post(url, headers=None, json=None, timeout=None):
            bodies.append(json)
            data = self.fx['listing'] if len(bodies) == 1 else self.fx['end']
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: copy.deepcopy(data))
        crawler._session = SimpleNamespace(post=post)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            crawler.crawl(limit=10)
        return captured, bodies

    def adapter(self):
        crawler, _ = load(SITE)
        return Adapter(SITE, self.plan.section('adapter'), None, crawler=crawler)

    def test_paper_and_identity_equal_original_crawl(self):
        expected, bodies = self.native()
        self.assertEqual(2, len(expected))
        adapter = self.adapter()
        resp = Response(200, {'Content-Type': 'application/json'}, json.dumps(self.fx['listing']), self.raw['listing']['url'])
        items = ListingParser(self.plan.listing['parser'], adapter=adapter).parse(resp).items
        self.assertEqual(3, len(items))
        actual = [p for p in (adapter.materialize(i, None) for i in items) if p is not None]
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])
        # The plan body is crawl()'s body (skip is the paging field).
        sent = dict(bodies[0])
        sent.pop('skip')
        self.assertEqual(sent, self.raw['listing']['params'])

    def test_api_key_only_from_the_environment(self):
        self.assertNotIn('api-key', {k.lower() for k in self.raw.get('request_headers', {})})
        self.assertEqual({'api-key': ENV}, self.raw['request_headers_env'])
        from crawler.enumeration.plan import PlanError
        with self.assertRaises(PlanError):
            resolve_headers(self.raw, {})
        self.assertEqual([HOST], header_hosts(self.raw))

    def test_empty_window_is_explicit_and_error_objects_are_not(self):
        parser = ListingParser(self.plan.listing['parser'], adapter=self.adapter())
        end = parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(self.fx['end']), 'https://x/'))
        self.assertEqual([], end.items)
        self.assertTrue(end.extra.get('explicit_empty'))
        for bad in ({'error': {'code': 'Forbidden'}}, {'value': None}, []):
            with self.assertRaises(PermanentFailure):
                parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(bad), 'https://x/'))

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
            body = json.loads(data)
            out = self.fx['listing'] if body.get('skip', 0) == 0 else self.fx['end']  # no skip = first window
            return Response(200, {'Content-Type': 'application/json'}, json.dumps(out), url)
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
        finally:
            env.close()
        self.assertIn(result['status'], ('complete', 'complete_with_review'), result)
        self.assertEqual(2, result['stats']['saved_new'])
        posts = [c for c in transport.calls if c[0] == 'POST']
        self.assertTrue(posts and all(c[2].get('api-key') == 'synthetic-test-key' for c in posts))
        self.assertEqual([0, 50], sorted({json.loads(c[3]).get('skip', 0) for c in posts})[:2])


if __name__ == '__main__':
    unittest.main()
