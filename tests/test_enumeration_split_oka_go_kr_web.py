"""oka-go-kr-web: ajax/list.do JSON rows + brdDetail.do JSON detail (ext:headers) through
the unchanged crawl() vs the plan adapter.

Both endpoints answer JSON only when ``Accept: application/json`` is sent (crawl() sends
it with curl); the plan declares it as a literal ``request_headers`` entry. The detail
conversion reuses ``_build_paper`` with the detail JSON the framework fetched.
"""
import contextlib
import copy
import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from crawler.enumeration.plan import header_hosts, resolve_headers
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks
from tests.collector_split_helpers import load, split_plan

SITE = 'oka-go-kr-web'


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = SITE

    def test_identity_is_board_num(self):
        papers = self.pipeline()
        self.assertEqual(self.fixture['expected_ids'], [p['post_number'] for p in papers])
        self.assertEqual(list(self.fixture['details']), [p['url'] for p in papers])

    def test_plan_declares_json_accept_header(self):
        site = self.plan['sites'][SITE]
        self.assertEqual({'Accept': 'application/json'}, resolve_headers(site, {}))
        self.assertEqual(['www.oka.go.kr'], header_hosts(site))
        self.assertEqual('pagingInfoVO.totalPageCount', site['listing']['last_page_path'])

    def test_fetch_detail_is_not_left_shadowed(self):
        crawler, _ = load(SITE)
        method = type(crawler)._fetch_detail
        self.pipeline()
        self.assertNotIn('_fetch_detail', vars(crawler))
        self.assertIs(method, type(crawler)._fetch_detail)


class AcceptTransport:
    """JSON only with Accept: application/json (like the site); records headers."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    @property
    def urls(self):
        return [c[1] for c in self.calls]

    def request(self, method, url, *, headers=None, data=None):
        headers = dict(headers or {})
        self.calls.append((method, url, headers))
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'')
        if headers.get('Accept') != 'application/json':
            return Response(404, {'Content-Type': 'text/html'}, '<html>not found</html>', url)
        if url in self.fixture['details']:
            return Response(200, {'Content-Type': 'application/json'},
                            json.dumps(self.fixture['details'][url], ensure_ascii=False), url)
        page = int(parse_qs(parts.query).get('currentPage', ['1'])[0])
        body = self.fixture['listing_body'] if page <= 1 else self.fixture['empty_body']
        return Response(200, {'Content-Type': 'application/json'}, body, url)


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = SITE

    def test_run_with_plan_headers_completes_and_sends_accept(self):
        from tests.enumeration_fakes import Env
        plan = copy.deepcopy(split_plan(SITE))
        plan['request_interval_s'] = 0
        transport = AcceptTransport(self.fixture)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, None, crawler=cls(env.conn), transport=transport,
                                 extra_headers=resolve_headers(plan), header_hosts=header_hosts(plan))
            docs = env.docs()
        finally:
            env.close()
        self.assertEqual('complete', result['status'], result)
        self.assertEqual(sorted(self.fixture['expected_ids']), sorted(d['post_number'] for d in docs))
        sent = [h.get('Accept') for _, u, h in transport.calls if not u.endswith('/robots.txt')]
        self.assertTrue(sent and all(a == 'application/json' for a in sent), sent)


if __name__ == '__main__':
    unittest.main()
