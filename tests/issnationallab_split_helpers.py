"""Shared offline checks for the issnationallab.org WordPress REST splits.

These crawlers take keyword-only constructor arguments (``__init__(self, **kwargs)``),
like the real runner uses (``cls(db_conn=conn)``); the generic ``run_split`` helper
constructs positionally, so the end-to-end runs here use a keyword variant.
No network, process or customer DB: a temporary SQLite Env and a fake transport only.
"""
import contextlib
import copy
import io
import json
from urllib.parse import parse_qs, urlsplit

from crawler.enumeration.http import Response
from tests.collector_split_helpers import load, split_fixture, split_plan, wp_past_last_page


def run_split_kw(site, *, end=wp_past_last_page, listing=None, listing_type=None, total=None, fixture=None):
    from tests.enumeration_fakes import Env
    fixture = fixture or split_fixture(site)
    plan = copy.deepcopy(split_plan(site))
    plan['request_interval_s'] = 0
    page_param = plan['listing'].get('page_param', 'page')
    start = plan['listing'].get('start_page', 1)

    def handler(url):
        parts = urlsplit(url)
        if parts.path == '/robots.txt':
            return Response(404, {}, b'')
        if url in fixture['details']:
            return Response(200, {'Content-Type': 'text/html'}, fixture['details'][url], url)
        query = parse_qs(parts.query)
        page = int(query[page_param][0]) if page_param in query else start
        if page <= start:
            headers = {'Content-Type': listing_type or fixture['content_type']}
            if total is not None:
                headers['X-WP-Total'] = str(total)
            return Response(200, headers, fixture['listing_body'] if listing is None else listing, url)
        return end(url)

    env = Env(site)
    try:
        _, module = load(site)
        cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == site)
        crawler = cls(db_conn=env.conn)
        with contextlib.redirect_stdout(io.StringIO()):
            result = env.run(plan, handler, crawler=crawler)
        return result, env.docs()
    finally:
        env.close()


class WpRunChecks:
    """End-to-end runs of the shipped example plan through the real SiteRunner.run."""
    SITE = None

    def short_fixture(self):
        f = split_fixture(self.SITE)
        return json.loads(json.dumps(f, ensure_ascii=False).replace(f['long_text'], 'Short.'))

    def test_shipped_plan_completes_end_to_end(self):
        result, docs = run_split_kw(self.SITE, total=2)
        self.assertEqual('complete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('last_page_'), result)
        self.assertEqual(2, len(docs))
        self.assertEqual(2, len({d['post_number'] for d in docs}))
        self.assertEqual(2, len({d['meta_url'] for d in docs}))

    def test_short_paper_does_not_silently_become_complete(self):
        result, docs = run_split_kw(self.SITE, fixture=self.short_fixture(), total=2)
        self.assertEqual('complete_with_review', result['status'], result)
        self.assertEqual([], docs)

    def test_error_listing_ends_incomplete_as_listing_parse_error(self):
        cases = {
            'maintenance_page': ('<html><body><p>Site under maintenance</p></body></html>', 'text/html'),
            'json_error_object': (json.dumps({'code': 'rest_no_route', 'message': 'No route',
                                              'data': {'status': 404}}), 'application/json'),
        }
        for name, (body, content_type) in cases.items():
            with self.subTest(case=name):
                result, docs = run_split_kw(self.SITE, listing=body, listing_type=content_type)
                self.assertEqual('incomplete', result['status'], result)
                self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
                self.assertEqual([], docs)

    def test_unverified_400_is_not_an_end(self):
        """A 400 without WordPress' rest_post_invalid_page_number body must not end the run as complete."""
        bad = lambda url: Response(400, {'Content-Type': 'text/html'}, '<html>Bad request</html>', url)
        result, _ = run_split_kw(self.SITE, end=bad, total=2)
        self.assertNotEqual('complete', result['status'], result)
