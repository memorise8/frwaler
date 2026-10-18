"""L3b-20261018 (ext:post_listing): enumeration split for library-kwrwater-nl-en.

The listing is the site's read-only search POST (admin-ajax.php, action=searchPublications,
25 rows). The site's own script selects pages with the POST field ``current``; crawl()
sends ``paged``, which the server ignores. A page past the end answers success with a
response without "documents" (crawl() stops on the KeyError). The paper conversion is
crawl()'s, copied verbatim (external_id = post_number = Elasticsearch id). Memory only."""
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
from urllib.parse import parse_qs

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import load

SITE = 'library-kwrwater-nl-en'
ROOT = Path(__file__).resolve().parents[1]
AJAX = 'https://library.kwrwater.nl/wp/wp-admin/admin-ajax.php'


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
        self.plan = site_plan(plan_doc(), SITE, registered=[SITE])
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, side_effect=AssertionError('I/O attempted')); p.start(); self.addCleanup(p.stop)

    def native(self):
        crawler, _ = load(SITE)
        captured, posts = [], []
        crawler._delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))

        def post(url, data=None, timeout=None):
            posts.append(list(data))
            body = self.fx['listing'] if len(posts) == 1 else self.fx['end']
            return SimpleNamespace(content=json.dumps(body).encode(), raise_for_status=lambda: None)
        crawler._session = SimpleNamespace(post=post)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=10)
        self.assertEqual(len(captured), result)
        return captured

    def adapter(self):
        crawler, _ = load(SITE)
        return Adapter(SITE, self.plan.section('adapter'), None, crawler=crawler)

    def pipeline(self, body=None):
        adapter = self.adapter()
        resp = Response(200, {'Content-Type': 'application/json'}, json.dumps(body or self.fx['listing']), AJAX)
        items = ListingParser(self.plan.listing['parser'], adapter=adapter).parse(resp).items
        return items, [p for p in (adapter.materialize(i, None) for i in items) if p is not None]

    def test_paper_and_identity_equal_original_crawl(self):
        expected = self.native()
        self.assertEqual(['900001', '900002', '900004'], [p['external_id'] for p in expected])
        items, actual = self.pipeline()
        self.assertEqual(4, len(items))  # the short-abstract row is listed, then skipped like crawl()
        self.assertEqual([clean_paper(p) for p in expected], actual)
        self.assertEqual([paper_identity(p) for p in expected], [paper_identity(p) for p in actual])

    def test_page_past_the_end_is_explicit_and_errors_are_not(self):
        parser = ListingParser(self.plan.listing['parser'], adapter=self.adapter())
        end = parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(self.fx['end']), AJAX))
        self.assertEqual([], end.items)
        self.assertTrue(end.extra.get('explicit_empty'))
        for bad in ({'success': False, 'data': 0}, {'success': True, 'data': {'response': {}}}, []):
            with self.assertRaises(PermanentFailure):
                parser.parse(Response(200, {'Content-Type': 'application/json'}, json.dumps(bad), AJAX))

    def test_original_methods_are_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        cls = next(x for x in ast.parse(code).body if isinstance(x, ast.ClassDef))
        actual = {n.name: hashlib.sha256(ast.get_source_segment(code, n).encode()).hexdigest()
                  for n in cls.body if isinstance(n, ast.FunctionDef)}
        for name, digest in self.fx['original_method_source_sha256'].items():
            self.assertEqual(digest, actual[name], name)

    def test_plan_posts_read_only_search_and_completes(self):
        from tests.enumeration_fakes import Env
        site = copy.deepcopy(plan_doc()['sites'][SITE])
        site['request_interval_s'] = 0
        self.assertEqual(('POST', True, 'form'), (site['listing']['method'], site['listing']['idempotent'],
                                                  site['listing']['body_type']))

        def handle(method, url, data):
            if url.endswith('/robots.txt'):
                return Response(200, {'Content-Type': 'text/plain'},
                                b'User-agent: *\nDisallow: /wp/wp-admin/\nAllow: /wp/wp-admin/admin-ajax.php\n', url)
            self.assertEqual(('POST', AJAX), (method, url))
            form = parse_qs(data.decode())
            self.assertEqual(['searchPublications'], form['action'])
            self.assertEqual(4, len(form['document_type[]']))
            body = self.fx['listing'] if form.get('current', ['1']) == ['1'] or 'current' not in form else self.fx['end']
            return Response(200, {'Content-Type': 'application/json'}, json.dumps(body), url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            transport = Recording(handle)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, None, crawler=cls(env.conn, 0), transport=transport)
            docs = env.docs()
        finally:
            env.close()
        # The short-abstract row crawl() skips is ledgered (empty_item) for review, never saved.
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual((3, 1), (result['stats']['saved_new'], result['stats']['failed_permanent']))
        self.assertEqual(['900001', '900002', '900004'], sorted(d['post_number'] for d in docs))
        self.assertTrue(all(c[0] == 'POST' for c in transport.calls if not c[1].endswith('/robots.txt')))


if __name__ == '__main__':
    unittest.main()
